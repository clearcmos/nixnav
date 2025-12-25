//! NixNav Daemon - High-performance file indexing daemon
//!
//! Uses trigram posting lists for instant substring search across millions of files.
//! Supports inotify for real-time local updates and periodic scanning for network mounts.

use std::collections::{HashMap, HashSet};
use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::{Arc, RwLock};
use std::time::{Duration, UNIX_EPOCH};
use std::{fs, thread};
use std::sync::mpsc::{channel, Sender};

use notify::{Config, RecommendedWatcher, RecursiveMode, Watcher, Event};
use rusqlite::{Connection, params};
use serde::{Deserialize, Serialize};
use tracing::{debug, error, info, warn};
use walkdir::WalkDir;

// Database operations sent through a channel to serialize access
enum DbOp {
    SaveFile(FileEntry),
    RemoveFile(String),
    SaveBookmark(Bookmark),
    ClearFilesUnder(String),
}

// ============================================================================
// Configuration
// ============================================================================

const SOCKET_PATH: &str = "/run/user/1000/nixnav-daemon.sock";
const DB_PATH: &str = ".local/share/nixnav/index.db";
const MAX_RESULTS: usize = 2000;
const NETWORK_SCAN_INTERVAL_SECS: u64 = 300; // 5 minutes

// Binary extensions to exclude in edit mode
const BINARY_EXTENSIONS: &[&str] = &[
    "png", "jpg", "jpeg", "gif", "bmp", "ico", "webp", "svg", "tiff", "raw",
    "mp3", "mp4", "wav", "avi", "mkv", "mov", "flac", "ogg", "m4a", "aac",
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
    "zip", "tar", "gz", "bz2", "xz", "7z", "rar", "zst",
    "exe", "dll", "so", "dylib", "a", "o", "obj",
    "bin", "dat", "db", "sqlite", "sqlite3",
    "ttf", "otf", "woff", "woff2", "eot",
    "class", "jar", "war", "pyc", "pyo", "whl",
    "min.js", "min.css",
];

// Patterns to always exclude
const EXCLUDE_PATTERNS: &[&str] = &[
    ".git", "node_modules", "__pycache__", ".cache", ".npm", ".cargo",
    "target", "build", "dist", ".next", ".nuxt", ".Trash", "Trash",
];

// ============================================================================
// Data Structures
// ============================================================================

#[derive(Debug, Clone, Serialize, Deserialize)]
struct FileEntry {
    id: u32,
    path: String,
    is_dir: bool,
    mtime: i64,
    size: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct Bookmark {
    name: String,
    path: String,
    is_network: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct SearchRequest {
    bookmark_path: String,
    mode: String,  // "edit", "gotofile", "gotodir", "all"
    query: String,
    extension: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct SearchAllRequest {
    bookmark_paths: Vec<String>,  // Empty = search all indexed files
    query: String,
    extension: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct SearchAllResult {
    path: String,
    is_dir: bool,
    mtime: i64,
    bookmark: String,  // Which bookmark this result belongs to
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct SearchResult {
    path: String,
    is_dir: bool,
    mtime: i64,
}

#[derive(Debug, Serialize, Deserialize)]
struct SearchResponse {
    results: Vec<SearchResult>,
    total_indexed: usize,
    search_time_ms: u64,
}

/// Trigram index for fast substring search
struct TrigramIndex {
    /// Trigram -> set of file IDs containing this trigram
    trigrams: HashMap<[u8; 3], HashSet<u32>>,
    /// File ID -> FileEntry
    files: HashMap<u32, FileEntry>,
    /// Path -> File ID (for quick lookups during updates)
    path_to_id: HashMap<String, u32>,
    /// Next available file ID
    next_id: u32,
    /// Bookmarks being indexed
    bookmarks: Vec<Bookmark>,
}

impl TrigramIndex {
    fn new() -> Self {
        Self {
            trigrams: HashMap::new(),
            files: HashMap::new(),
            path_to_id: HashMap::new(),
            next_id: 1,
            bookmarks: Vec::new(),
        }
    }

    /// Extract trigrams from a string (lowercase for case-insensitive search)
    fn extract_trigrams(s: &str) -> Vec<[u8; 3]> {
        let lower = s.to_lowercase();
        let bytes = lower.as_bytes();
        if bytes.len() < 3 {
            return Vec::new();
        }
        bytes.windows(3)
            .map(|w| [w[0], w[1], w[2]])
            .collect()
    }

    /// Add a file to the index
    fn add(&mut self, path: String, is_dir: bool, mtime: i64, size: u64) -> u32 {
        // Check if already exists
        if let Some(&existing_id) = self.path_to_id.get(&path) {
            // Update existing entry
            if let Some(entry) = self.files.get_mut(&existing_id) {
                entry.mtime = mtime;
                entry.size = size;
            }
            return existing_id;
        }

        let id = self.next_id;
        self.next_id += 1;

        // Extract filename for trigram indexing (search matches against filename, not full path)
        let filename = Path::new(&path)
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or(&path);

        // Index trigrams from filename
        for trigram in Self::extract_trigrams(filename) {
            self.trigrams.entry(trigram).or_default().insert(id);
        }

        // Also index path components for path-based search
        for component in path.split('/').filter(|s| !s.is_empty()) {
            for trigram in Self::extract_trigrams(component) {
                self.trigrams.entry(trigram).or_default().insert(id);
            }
        }

        let entry = FileEntry { id, path: path.clone(), is_dir, mtime, size };
        self.files.insert(id, entry);
        self.path_to_id.insert(path, id);

        id
    }

    /// Remove a file from the index
    fn remove(&mut self, path: &str) {
        if let Some(id) = self.path_to_id.remove(path) {
            if let Some(entry) = self.files.remove(&id) {
                let filename = Path::new(&entry.path)
                    .file_name()
                    .and_then(|n| n.to_str())
                    .unwrap_or(&entry.path);

                // Remove from trigram index
                for trigram in Self::extract_trigrams(filename) {
                    if let Some(set) = self.trigrams.get_mut(&trigram) {
                        set.remove(&id);
                    }
                }
                for component in entry.path.split('/').filter(|s| !s.is_empty()) {
                    for trigram in Self::extract_trigrams(component) {
                        if let Some(set) = self.trigrams.get_mut(&trigram) {
                            set.remove(&id);
                        }
                    }
                }
            }
        }
    }

    /// Search for files matching the query
    fn search(&self, req: &SearchRequest) -> Vec<SearchResult> {
        let query_lower = req.query.to_lowercase();
        let trigrams = Self::extract_trigrams(&query_lower);

        // Get candidate file IDs from trigram intersection
        let candidates: HashSet<u32> = if trigrams.is_empty() {
            // Empty or short query - return all files under bookmark
            self.files.keys().copied().collect()
        } else {
            // Intersect posting lists for all trigrams
            let mut iter = trigrams.iter();
            let first = iter.next().unwrap();
            let mut candidates = self.trigrams
                .get(first)
                .cloned()
                .unwrap_or_default();

            for trigram in iter {
                if let Some(set) = self.trigrams.get(trigram) {
                    candidates = candidates.intersection(set).copied().collect();
                } else {
                    // Trigram not found - no matches
                    return Vec::new();
                }
                if candidates.is_empty() {
                    return Vec::new();
                }
            }
            candidates
        };

        // Filter candidates
        let is_dir_mode = req.mode == "gotodir";
        let is_edit_mode = req.mode == "edit";
        let is_all_mode = req.mode == "all";
        let bookmark_path = &req.bookmark_path;

        let mut results: Vec<SearchResult> = candidates
            .into_iter()
            .filter_map(|id| self.files.get(&id))
            .filter(|entry| {
                // Must be under the bookmark path
                if !entry.path.starts_with(bookmark_path) {
                    return false;
                }

                // Mode filter (skip for "all" mode - include both files and dirs)
                if !is_all_mode {
                    if is_dir_mode && !entry.is_dir {
                        return false;
                    }
                    if !is_dir_mode && entry.is_dir {
                        return false;
                    }
                }

                // Binary extension filter for edit mode (skip for "all" mode)
                if is_edit_mode {
                    if let Some(ext) = Path::new(&entry.path).extension().and_then(|e| e.to_str()) {
                        if BINARY_EXTENSIONS.contains(&ext.to_lowercase().as_str()) {
                            return false;
                        }
                    }
                }

                // Extension filter
                if let Some(ref ext_filter) = req.extension {
                    if let Some(ext) = Path::new(&entry.path).extension().and_then(|e| e.to_str()) {
                        if ext.to_lowercase() != ext_filter.to_lowercase() {
                            return false;
                        }
                    } else {
                        return false;
                    }
                }

                // Verify actual substring match (trigrams can have false positives)
                if !req.query.is_empty() {
                    let path_lower = entry.path.to_lowercase();
                    if !path_lower.contains(&query_lower) {
                        return false;
                    }
                }

                true
            })
            .map(|entry| SearchResult {
                path: entry.path.clone(),
                is_dir: entry.is_dir,
                mtime: entry.mtime,
            })
            .collect();

        // Sort by mtime descending (most recent first)
        results.sort_by(|a, b| b.mtime.cmp(&a.mtime));

        // Limit results
        results.truncate(MAX_RESULTS);

        results
    }

    /// Search all bookmarks at once (much faster than multiple searches)
    fn search_all(&self, req: &SearchAllRequest) -> Vec<SearchAllResult> {
        let query_lower = req.query.to_lowercase();
        let trigrams = Self::extract_trigrams(&query_lower);

        // Build bookmark path -> name mapping
        let bookmark_map: HashMap<&str, &str> = self.bookmarks
            .iter()
            .map(|b| (b.path.as_str(), b.name.as_str()))
            .collect();

        // Determine which bookmark paths to search
        let search_paths: Vec<&str> = if req.bookmark_paths.is_empty() {
            // Search all bookmarks
            self.bookmarks.iter().map(|b| b.path.as_str()).collect()
        } else {
            req.bookmark_paths.iter().map(|s| s.as_str()).collect()
        };

        // Get candidate file IDs from trigram intersection
        let candidates: HashSet<u32> = if trigrams.is_empty() {
            // Empty or short query - return all files
            self.files.keys().copied().collect()
        } else {
            // Intersect posting lists for all trigrams
            let mut iter = trigrams.iter();
            let first = iter.next().unwrap();
            let mut candidates = self.trigrams
                .get(first)
                .cloned()
                .unwrap_or_default();

            for trigram in iter {
                if let Some(set) = self.trigrams.get(trigram) {
                    candidates = candidates.intersection(set).copied().collect();
                } else {
                    return Vec::new();
                }
                if candidates.is_empty() {
                    return Vec::new();
                }
            }
            candidates
        };

        // Filter candidates and determine bookmark for each
        let mut results: Vec<SearchAllResult> = candidates
            .into_iter()
            .filter_map(|id| self.files.get(&id))
            .filter_map(|entry| {
                // Find which bookmark this file belongs to
                let bookmark_name = search_paths.iter()
                    .find(|&bp| entry.path.starts_with(bp))
                    .and_then(|bp| bookmark_map.get(bp).copied())?;

                // Extension filter
                if let Some(ref ext_filter) = req.extension {
                    if let Some(ext) = Path::new(&entry.path).extension().and_then(|e| e.to_str()) {
                        if ext.to_lowercase() != ext_filter.to_lowercase() {
                            return None;
                        }
                    } else {
                        return None;
                    }
                }

                // Verify actual substring match
                if !req.query.is_empty() {
                    let path_lower = entry.path.to_lowercase();
                    if !path_lower.contains(&query_lower) {
                        return None;
                    }
                }

                Some(SearchAllResult {
                    path: entry.path.clone(),
                    is_dir: entry.is_dir,
                    mtime: entry.mtime,
                    bookmark: bookmark_name.to_string(),
                })
            })
            .collect();

        // Sort by mtime descending
        results.sort_by(|a, b| b.mtime.cmp(&a.mtime));
        results.truncate(MAX_RESULTS);
        results
    }

    fn file_count(&self) -> usize {
        self.files.len()
    }
}

// ============================================================================
// Database Persistence (runs in dedicated thread)
// ============================================================================

struct Database {
    conn: Connection,
}

impl Database {
    fn open() -> rusqlite::Result<Self> {
        let home = dirs::home_dir().expect("No home directory");
        let db_path = home.join(DB_PATH);

        if let Some(parent) = db_path.parent() {
            fs::create_dir_all(parent).ok();
        }

        let conn = Connection::open(&db_path)?;

        conn.execute_batch(r#"
            PRAGMA journal_mode = WAL;
            PRAGMA synchronous = NORMAL;
            PRAGMA cache_size = -64000;
            PRAGMA temp_store = MEMORY;

            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY,
                path TEXT UNIQUE NOT NULL,
                is_dir INTEGER NOT NULL,
                mtime INTEGER NOT NULL,
                size INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS bookmarks (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                path TEXT UNIQUE NOT NULL,
                is_network INTEGER NOT NULL,
                last_scan INTEGER
            );

            CREATE INDEX IF NOT EXISTS idx_files_path ON files(path);
            CREATE INDEX IF NOT EXISTS idx_files_mtime ON files(mtime);
        "#)?;

        Ok(Self { conn })
    }

    fn load_into_index(&self, index: &mut TrigramIndex) -> rusqlite::Result<usize> {
        let mut stmt = self.conn.prepare("SELECT id, path, is_dir, mtime, size FROM files")?;
        let mut count = 0;

        let rows = stmt.query_map([], |row| {
            Ok(FileEntry {
                id: row.get(0)?,
                path: row.get(1)?,
                is_dir: row.get::<_, i32>(2)? != 0,
                mtime: row.get(3)?,
                size: row.get(4)?,
            })
        })?;

        for entry in rows {
            let entry = entry?;
            let id = entry.id;

            if id >= index.next_id {
                index.next_id = id + 1;
            }

            let filename = Path::new(&entry.path)
                .file_name()
                .and_then(|n| n.to_str())
                .unwrap_or(&entry.path);

            for trigram in TrigramIndex::extract_trigrams(filename) {
                index.trigrams.entry(trigram).or_default().insert(id);
            }
            for component in entry.path.split('/').filter(|s| !s.is_empty()) {
                for trigram in TrigramIndex::extract_trigrams(component) {
                    index.trigrams.entry(trigram).or_default().insert(id);
                }
            }

            index.path_to_id.insert(entry.path.clone(), id);
            index.files.insert(id, entry);
            count += 1;
        }

        let mut stmt = self.conn.prepare("SELECT name, path, is_network FROM bookmarks")?;
        let bookmarks = stmt.query_map([], |row| {
            Ok(Bookmark {
                name: row.get(0)?,
                path: row.get(1)?,
                is_network: row.get::<_, i32>(2)? != 0,
            })
        })?;

        for bookmark in bookmarks {
            index.bookmarks.push(bookmark?);
        }

        Ok(count)
    }

    fn save_file(&self, entry: &FileEntry) {
        if let Err(e) = self.conn.execute(
            "INSERT OR REPLACE INTO files (id, path, is_dir, mtime, size) VALUES (?1, ?2, ?3, ?4, ?5)",
            params![entry.id, entry.path, entry.is_dir as i32, entry.mtime, entry.size],
        ) {
            warn!("Failed to save file: {}", e);
        }
    }

    fn remove_file(&self, path: &str) {
        if let Err(e) = self.conn.execute("DELETE FROM files WHERE path = ?1", params![path]) {
            warn!("Failed to remove file: {}", e);
        }
    }

    fn save_bookmark(&self, bookmark: &Bookmark) {
        if let Err(e) = self.conn.execute(
            "INSERT OR REPLACE INTO bookmarks (name, path, is_network) VALUES (?1, ?2, ?3)",
            params![bookmark.name, bookmark.path, bookmark.is_network as i32],
        ) {
            warn!("Failed to save bookmark: {}", e);
        }
    }

    fn clear_files_under(&self, path: &str) {
        if let Err(e) = self.conn.execute(
            "DELETE FROM files WHERE path LIKE ?1",
            params![format!("{}%", path)],
        ) {
            warn!("Failed to clear files: {}", e);
        }
    }

    fn process_op(&self, op: DbOp) {
        match op {
            DbOp::SaveFile(entry) => self.save_file(&entry),
            DbOp::RemoveFile(path) => self.remove_file(&path),
            DbOp::SaveBookmark(bookmark) => self.save_bookmark(&bookmark),
            DbOp::ClearFilesUnder(path) => self.clear_files_under(&path),
        }
    }
}

/// Start database thread that processes operations from a channel
fn start_db_thread(db: Database) -> Sender<DbOp> {
    let (tx, rx) = channel::<DbOp>();

    thread::spawn(move || {
        for op in rx {
            db.process_op(op);
        }
    });

    tx
}

// ============================================================================
// File Scanner
// ============================================================================

fn is_network_mount(path: &Path) -> bool {
    let path_str = path.to_string_lossy();

    if path_str.starts_with("/mnt/") ||
       path_str.starts_with("/media/") ||
       path_str.starts_with("/net/") {
        if let Ok(mounts) = fs::read_to_string("/proc/mounts") {
            for line in mounts.lines() {
                let parts: Vec<&str> = line.split_whitespace().collect();
                if parts.len() >= 3 {
                    let mount_point = parts[1];
                    let fs_type = parts[2];

                    if path_str.starts_with(mount_point) {
                        return matches!(fs_type, "nfs" | "nfs4" | "cifs" | "smb" | "smbfs" | "fuse.sshfs");
                    }
                }
            }
        }
    }
    false
}

fn should_exclude(path: &Path) -> bool {
    if let Some(name) = path.file_name().and_then(|n| n.to_str()) {
        // Exact match or prefix match for .Trash-* folders
        EXCLUDE_PATTERNS.iter().any(|p| name == *p || (p == &".Trash" && name.starts_with(".Trash")))
    } else {
        false
    }
}

fn get_mtime(path: &Path) -> i64 {
    path.metadata()
        .and_then(|m| m.modified())
        .map(|t| t.duration_since(UNIX_EPOCH).unwrap_or_default().as_secs() as i64)
        .unwrap_or(0)
}

fn get_size(path: &Path) -> u64 {
    path.metadata().map(|m| m.len()).unwrap_or(0)
}

fn scan_directory(
    root: &Path,
    index: &Arc<RwLock<TrigramIndex>>,
    db_tx: &Sender<DbOp>,
) -> usize {
    let mut count = 0;
    let start = std::time::Instant::now();

    info!("Scanning directory: {}", root.display());

    let walker = WalkDir::new(root)
        .follow_links(false)
        .into_iter()
        .filter_entry(|e| !should_exclude(e.path()));

    for entry in walker.filter_map(|e| e.ok()) {
        let path = entry.path();
        let path_str = path.to_string_lossy().to_string();
        let is_dir = entry.file_type().is_dir();
        let mtime = get_mtime(path);
        let size = if is_dir { 0 } else { get_size(path) };

        let id = {
            let mut idx = index.write().unwrap();
            idx.add(path_str.clone(), is_dir, mtime, size)
        };

        let entry = FileEntry { id, path: path_str, is_dir, mtime, size };
        let _ = db_tx.send(DbOp::SaveFile(entry));
        count += 1;
    }

    let elapsed = start.elapsed();
    info!("Scanned {} files in {:?}", count, elapsed);

    count
}

// ============================================================================
// File Watcher (inotify)
// ============================================================================

fn start_watcher(
    paths: Vec<PathBuf>,
    index: Arc<RwLock<TrigramIndex>>,
    db_tx: Sender<DbOp>,
) -> notify::Result<RecommendedWatcher> {
    let index_clone = index.clone();
    let db_tx_clone = db_tx.clone();

    let mut watcher = RecommendedWatcher::new(
        move |res: Result<Event, notify::Error>| {
            match res {
                Ok(event) => handle_fs_event(event, &index_clone, &db_tx_clone),
                Err(e) => warn!("Watch error: {:?}", e),
            }
        },
        Config::default().with_poll_interval(Duration::from_secs(2)),
    )?;

    for path in paths {
        if !is_network_mount(&path) {
            info!("Watching (inotify): {}", path.display());
            watcher.watch(&path, RecursiveMode::Recursive)?;
        } else {
            info!("Skipping inotify for network mount: {}", path.display());
        }
    }

    Ok(watcher)
}

fn handle_fs_event(event: Event, index: &Arc<RwLock<TrigramIndex>>, db_tx: &Sender<DbOp>) {
    use notify::EventKind::*;

    match event.kind {
        Create(_) | Modify(_) => {
            for path in event.paths {
                if should_exclude(&path) {
                    continue;
                }
                if let Ok(meta) = path.metadata() {
                    let path_str = path.to_string_lossy().to_string();
                    let is_dir = meta.is_dir();
                    let mtime = get_mtime(&path);
                    let size = if is_dir { 0 } else { meta.len() };

                    let id = {
                        let mut idx = index.write().unwrap();
                        idx.add(path_str.clone(), is_dir, mtime, size)
                    };

                    let entry = FileEntry { id, path: path_str, is_dir, mtime, size };
                    let _ = db_tx.send(DbOp::SaveFile(entry));

                    debug!("Indexed: {}", path.display());
                }
            }
        }
        Remove(_) => {
            for path in event.paths {
                let path_str = path.to_string_lossy().to_string();

                {
                    let mut idx = index.write().unwrap();
                    idx.remove(&path_str);
                }

                let _ = db_tx.send(DbOp::RemoveFile(path_str.clone()));

                debug!("Removed: {}", path.display());
            }
        }
        _ => {}
    }
}

// ============================================================================
// Network Mount Scanner (periodic)
// ============================================================================

fn start_network_scanner(
    paths: Vec<PathBuf>,
    index: Arc<RwLock<TrigramIndex>>,
    db_tx: Sender<DbOp>,
) {
    let network_paths: Vec<PathBuf> = paths.into_iter()
        .filter(|p| is_network_mount(p))
        .collect();

    if network_paths.is_empty() {
        return;
    }

    thread::spawn(move || {
        loop {
            for path in &network_paths {
                info!("Periodic scan of network mount: {}", path.display());
                scan_directory(path, &index, &db_tx);
            }
            thread::sleep(Duration::from_secs(NETWORK_SCAN_INTERVAL_SECS));
        }
    });
}

// ============================================================================
// Integrity Checker (detects deleted files)
// ============================================================================

const INTEGRITY_CHECK_INTERVAL_SECS: u64 = 60;  // Check every minute
const INTEGRITY_BATCH_SIZE: usize = 5000;       // Files per check cycle

fn start_integrity_checker(
    index: Arc<RwLock<TrigramIndex>>,
    db_tx: Sender<DbOp>,
) {
    thread::spawn(move || {
        let mut offset = 0;

        loop {
            thread::sleep(Duration::from_secs(INTEGRITY_CHECK_INTERVAL_SECS));

            // Get a batch of file paths to check
            let paths_to_check: Vec<String> = {
                let idx = index.read().unwrap();
                let all_paths: Vec<_> = idx.files.values()
                    .map(|f| f.path.clone())
                    .collect();

                if all_paths.is_empty() {
                    continue;
                }

                // Wrap around if we've gone past the end
                if offset >= all_paths.len() {
                    offset = 0;
                }

                let end = (offset + INTEGRITY_BATCH_SIZE).min(all_paths.len());
                let batch = all_paths[offset..end].to_vec();
                offset = end;
                batch
            };

            // Check which files no longer exist
            let mut removed_count = 0;
            for path_str in paths_to_check {
                let path = Path::new(&path_str);
                if !path.exists() {
                    // File was deleted - remove from index
                    {
                        let mut idx = index.write().unwrap();
                        idx.remove(&path_str);
                    }
                    let _ = db_tx.send(DbOp::RemoveFile(path_str));
                    removed_count += 1;
                }
            }

            if removed_count > 0 {
                info!("Integrity check: removed {} stale entries", removed_count);
            }
        }
    });
}

// ============================================================================
// IPC Server
// ============================================================================

fn handle_client(
    stream: UnixStream,
    index: &Arc<RwLock<TrigramIndex>>,
    db_tx: &Sender<DbOp>,
) {
    let mut reader = BufReader::new(&stream);
    let mut writer = &stream;

    loop {
        let mut line = String::new();
        match reader.read_line(&mut line) {
            Ok(0) => break,
            Ok(_) => {
                let line = line.trim();
                if line.is_empty() {
                    continue;
                }

                debug!("Received: {}", line);

                let response = if line.starts_with("SEARCH_ALL ") {
                    // Fast unified search across all bookmarks
                    let json = &line[11..];
                    match serde_json::from_str::<SearchAllRequest>(json) {
                        Ok(req) => {
                            let start = std::time::Instant::now();
                            let results = index.read().unwrap().search_all(&req);
                            let elapsed = start.elapsed().as_millis() as u64;
                            let total = index.read().unwrap().file_count();

                            // Return as JSON with results array
                            let resp = serde_json::json!({
                                "results": results,
                                "total_indexed": total,
                                "search_time_ms": elapsed
                            });
                            serde_json::to_string(&resp).unwrap_or_else(|_| "{}".to_string())
                        }
                        Err(e) => format!(r#"{{"error": "{}"}}"#, e),
                    }
                } else if line.starts_with("SEARCH ") {
                    let json = &line[7..];
                    match serde_json::from_str::<SearchRequest>(json) {
                        Ok(req) => {
                            let start = std::time::Instant::now();
                            let results = index.read().unwrap().search(&req);
                            let elapsed = start.elapsed().as_millis() as u64;
                            let total = index.read().unwrap().file_count();

                            let resp = SearchResponse {
                                results,
                                total_indexed: total,
                                search_time_ms: elapsed,
                            };
                            serde_json::to_string(&resp).unwrap_or_else(|_| "{}".to_string())
                        }
                        Err(e) => format!(r#"{{"error": "{}"}}"#, e),
                    }
                } else if line.starts_with("ADD_BOOKMARK ") {
                    let json = &line[13..];
                    match serde_json::from_str::<Bookmark>(json) {
                        Ok(bookmark) => {
                            let path = PathBuf::from(&bookmark.path);

                            let _ = db_tx.send(DbOp::SaveBookmark(bookmark.clone()));

                            let count = scan_directory(&path, index, db_tx);

                            index.write().unwrap().bookmarks.push(bookmark);

                            format!(r#"{{"status": "ok", "indexed": {}}}"#, count)
                        }
                        Err(e) => format!(r#"{{"error": "{}"}}"#, e),
                    }
                } else if line == "STATS" {
                    let idx = index.read().unwrap();
                    format!(
                        r#"{{"files": {}, "trigrams": {}, "bookmarks": {}}}"#,
                        idx.files.len(),
                        idx.trigrams.len(),
                        idx.bookmarks.len()
                    )
                } else if line.starts_with("RESCAN ") {
                    let path = line[7..].trim();
                    let path_buf = PathBuf::from(path);

                    {
                        let mut idx = index.write().unwrap();
                        let to_remove: Vec<String> = idx.files.values()
                            .filter(|f| f.path.starts_with(path))
                            .map(|f| f.path.clone())
                            .collect();
                        for p in to_remove {
                            idx.remove(&p);
                        }
                    }
                    let _ = db_tx.send(DbOp::ClearFilesUnder(path.to_string()));

                    let count = scan_directory(&path_buf, index, db_tx);
                    format!(r#"{{"status": "ok", "indexed": {}}}"#, count)
                } else if line == "PING" {
                    r#"{"status": "pong"}"#.to_string()
                } else {
                    r#"{"error": "unknown command"}"#.to_string()
                };

                if let Err(e) = writeln!(writer, "{}", response) {
                    warn!("Failed to write response: {}", e);
                    break;
                }
            }
            Err(e) => {
                warn!("Read error: {}", e);
                break;
            }
        }
    }
}

fn start_server(index: Arc<RwLock<TrigramIndex>>, db_tx: Sender<DbOp>) {
    let _ = fs::remove_file(SOCKET_PATH);

    let listener = match UnixListener::bind(SOCKET_PATH) {
        Ok(l) => l,
        Err(e) => {
            error!("Failed to bind socket: {}", e);
            return;
        }
    };

    info!("Listening on {}", SOCKET_PATH);

    for stream in listener.incoming() {
        match stream {
            Ok(stream) => {
                let index = index.clone();
                let db_tx = db_tx.clone();
                thread::spawn(move || {
                    handle_client(stream, &index, &db_tx);
                });
            }
            Err(e) => {
                warn!("Accept error: {}", e);
            }
        }
    }
}

// ============================================================================
// Main
// ============================================================================

fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::from_default_env()
                .add_directive("nixnav_daemon=info".parse().unwrap())
        )
        .init();

    info!("NixNav Daemon starting...");

    // Open database and load into index
    let db = match Database::open() {
        Ok(db) => db,
        Err(e) => {
            error!("Failed to open database: {}", e);
            return;
        }
    };

    let index = Arc::new(RwLock::new(TrigramIndex::new()));

    let start = std::time::Instant::now();
    let loaded = {
        let mut idx = index.write().unwrap();
        db.load_into_index(&mut idx).unwrap_or(0)
    };
    info!("Loaded {} files from database in {:?}", loaded, start.elapsed());

    // Start database thread
    let db_tx = start_db_thread(db);

    // Default bookmark if none exist
    let bookmarks = index.read().unwrap().bookmarks.clone();
    let paths: Vec<PathBuf> = if bookmarks.is_empty() {
        let home = dirs::home_dir().expect("No home directory");
        info!("No bookmarks found, using home: {}", home.display());
        vec![home]
    } else {
        bookmarks.iter().map(|b| PathBuf::from(&b.path)).collect()
    };

    // Initial scan if database was empty
    if loaded == 0 {
        for path in &paths {
            scan_directory(path, &index, &db_tx);
        }
    }

    // Start file watcher for local paths
    let _watcher = start_watcher(paths.clone(), index.clone(), db_tx.clone());

    // Start periodic scanner for network mounts
    start_network_scanner(paths, index.clone(), db_tx.clone());

    // Start integrity checker (detects deleted files missed by inotify)
    start_integrity_checker(index.clone(), db_tx.clone());

    // Start IPC server (blocks)
    start_server(index, db_tx);
}
