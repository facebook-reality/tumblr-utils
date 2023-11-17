from typing import Iterator, TextIO

import contextlib
import time
import os
import sys

if sys.platform == 'darwin':
    import fcntl

from tempfile import NamedTemporaryFile

from tumblr_utils.constants import FILE_ENCODING


def mkdir(dir, recursive=False):
    if not os.path.exists(dir):
        try:
            if recursive:
                os.makedirs(dir)
            else:
                os.mkdir(dir)
        except FileExistsError:
            pass  # ignored


def file_path_to(*parts):
    return os.path.join(save_folder, *parts)  # TODO: figure out how to plug save_volder in here


def open_file(open_fn, parts):
    mkdir(file_path_to(*parts[:-1]), recursive=True)
    return open_fn(file_path_to(*parts))


def fsync(fd):
    if sys.platform == 'darwin':
        # Apple's fsync does not flush the drive write cache
        try:
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
        except OSError:
            pass  # fall back to fsync
        else:
            return
    os.fsync(fd)


class open_outfile:
    def __init__(self, mode, *parts, **kwargs):
        self._dest_path = open_file(lambda f: f, parts)
        dest_dirname, dest_basename = os.path.split(self._dest_path)

        self._partf = NamedTemporaryFile(mode, prefix='.{}.'.format(dest_basename), dir=dest_dirname, delete=False)
        # NB: open by name so name attribute is accurate
        self._f = open(self._partf.name, mode, **kwargs)

    def __enter__(self):
        return self._f

    def __exit__(self, exc_type, exc_value, tb):
        partf = self._partf
        self._f.close()

        if exc_type is not None:
            # roll back on exception; do not write partial files
            partf.close()
            os.unlink(partf.name)
            return

        # NamedTemporaryFile is created 0600, set mode to the usual 0644
        if os.name == 'posix':
            os.fchmod(partf.fileno(), 0o644)
        else:
            os.chmod(partf.name, 0o644)

        # Flush buffers and sync the inode
        partf.flush()
        fsync(partf)
        partf.close()

        # Move to final destination
        os.replace(partf.name, self._dest_path)


@contextlib.contextmanager
def open_text(*parts, mode='w') -> Iterator[TextIO]:
    assert 'b' not in mode
    with open_outfile(mode, *parts, encoding=FILE_ENCODING, errors='xmlcharrefreplace') as f:
        yield f


def strftime(fmt, t=None):
    if t is None:
        t = time.localtime()
    return time.strftime(fmt, t)
