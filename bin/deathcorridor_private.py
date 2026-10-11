"""deathcorridor_private: everything deathcorridor stores about your machine (what you follow, crash reports, kill log, AI choice)
is for you only: directories 0700, files 0600, whatever the user's umask is. Existing files are tightened on start."""
import os

DIR_MODE, FILE_MODE = 0o700, 0o600


def mkdir(path):
    os.makedirs(path, mode=DIR_MODE, exist_ok=True)
    try: os.chmod(path, DIR_MODE)
    except OSError: pass


def _open(path, flags):
    mkdir(os.path.dirname(path))
    fd = os.open(path, flags | os.O_CREAT, FILE_MODE)
    try: os.fchmod(fd, FILE_MODE)                # also fixes a file created earlier with looser permissions
    except OSError: pass
    return os.fdopen(fd, "a" if flags & os.O_APPEND else "w", encoding="utf-8")


def append(path, text):
    with _open(path, os.O_WRONLY | os.O_APPEND) as f: f.write(text)


def write(path, text):
    """Atomic: written to a private temp file, then moved over the target."""
    tmp = path + ".tmp"
    with _open(tmp, os.O_WRONLY | os.O_TRUNC) as f: f.write(text)
    os.replace(tmp, path)


def tighten(root):
    """chmod an existing tree: directories 0700, files 0600 (best effort, never raises)."""
    try:
        for base, dirs, files in os.walk(root):
            try: os.chmod(base, DIR_MODE)
            except OSError: pass
            for n in files:
                try: os.chmod(os.path.join(base, n), FILE_MODE)
                except OSError: pass
    except OSError: pass
