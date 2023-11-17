#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# standard Python library imports
import hashlib
import json
import locale
import multiprocessing
import os
import re
import shutil
import signal
import sys
import threading
import time
import warnings
from collections import defaultdict
from os.path import join, split, splitext
from posixpath import join as urlpathjoin
from types import ModuleType
from typing import (TYPE_CHECKING, DefaultDict, List, Optional, Set, TextIO,
                    Type, cast)
from xml.sax.saxutils import escape

from util import (AsyncCallable, FakeGenericMeta, MultiCondition, copyfile,
                  enospc, fdatasync, fsync, have_module, no_internet, opendir,
                  to_bytes)
from wget import HTTPError, Retry, WGError, WgetRetrieveWrapper, setup_wget, urlopen
from is_reblog import post_is_reblog

if TYPE_CHECKING:
    from typing_extensions import Literal
    from bs4 import Tag
else:
    class Literal(metaclass=FakeGenericMeta):
        pass
    Tag = None

try:
    from settings import DEFAULT_BLOGS
except ImportError:
    DEFAULT_BLOGS = []

# extra optional packages
try:
    import pyexiv2
except ImportError:
    if not TYPE_CHECKING:
        pyexiv2 = None

try:
    import jq
except ImportError:
    if not TYPE_CHECKING:
        jq = None

# NB: setup_urllib3_ssl has already been called by wget

try:
    import requests
except ImportError:
    if not TYPE_CHECKING:
        # Import pip._internal.download first to avoid a potential recursive import
        try:
            from pip._internal import download as _  # noqa: F401
        except ImportError:
            pass  # doesn't exist in pip 20.0+
        try:
            from pip._vendor import requests
        except ImportError:
            raise RuntimeError('The requests module is required. Please install it with pip or your package manager.')

try:
    import filetype
except ImportError:
    with warnings.catch_warnings(record=True) as catcher:
        import imghdr
        if any(w.category is DeprecationWarning for w in catcher):
            print('warning: filetype module not found, using deprecated imghdr', file=sys.stderr)

    # add another JPEG recognizer
    # see http://www.garykessler.net/library/file_sigs.html
    def test_jpg(h, f):
        if h[:3] == b'\xFF\xD8\xFF' and h[3] in b'\xDB\xE0\xE1\xE2\xE3':
            return 'jpeg'

    imghdr.tests.append(test_jpg)

    def guess_extension(f):
        ext = imghdr.what(f)
        if ext == 'jpeg':
            ext = 'jpg'
        return ext
else:
    def guess_extension(f):
        kind = filetype.guess(f)
        return kind.extension if kind else None

# Imported later if needed
ytdl_module: Optional[ModuleType] = None

# variable directory names, will be set in TumblrBackup.backup()
save_folder = ''
media_folder = ''

# constant? names
post_dir = 'posts'  # Not actually a constant, see bloxsom stuff
media_dir = 'media'  # Not a const
save_dir = '..'  # Not a const

blog_name = ''
post_ext = '.html'
have_custom_css = False

# Always retry on 503 or 504, but never on connect or 429, the latter handled specially
HTTP_RETRY = Retry(3, connect=False, status_forcelist=frozenset((503, 504)))
HTTP_RETRY.RETRY_AFTER_STATUS_CODES = frozenset((413,))  # type: ignore[misc]

# get your own API key at https://www.tumblr.com/oauth/apps
API_KEY = ''

# ensure the right date/time format
try:
    locale.setlocale(locale.LC_TIME, '')
except locale.Error:
    pass

PREV_MUST_MATCH_OPTIONS = ('likes', 'blosxom')
MEDIA_PATH_OPTIONS = ('dirs', 'hostdirs', 'image_names')
MUST_MATCH_OPTIONS = PREV_MUST_MATCH_OPTIONS + MEDIA_PATH_OPTIONS
BACKUP_CHANGING_OPTIONS = (
    'save_images', 'save_video', 'save_video_tumblr', 'save_audio', 'save_notes', 'copy_notes', 'notes_limit', 'json',
    'count', 'skip', 'period', 'request', 'filter', 'no_reblog', 'only_reblog', 'exif', 'prev_archives',
    'use_server_timestamps', 'user_agent', 'no_get', 'internet_archive', 'media_list', 'idents',
)

wget_retrieve: Optional[WgetRetrieveWrapper] = None
main_thread_lock = threading.RLock()
multicond = MultiCondition(main_thread_lock)
disable_note_scraper: Set[str] = set()
disablens_lock = threading.Lock()
downloading_media: Set[str] = set()
downloading_media_cond = threading.Condition()


def load_bs4(reason):
    sys.modules['soupsieve'] = ()  # type: ignore[assignment]
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        raise RuntimeError("Cannot {} without module 'bs4'".format(reason))
    try:
        import lxml  # noqa: F401
    except ImportError:
        raise RuntimeError("Cannot {} without module 'lxml'".format(reason))
    return BeautifulSoup


logger = Logger()


def get_api_url(account):
    """construct the tumblr API URL"""
    global blog_name
    blog_name = account
    if any(c in account for c in '/\\') or account in ('.', '..'):
        raise ValueError('Invalid blog name: {!r}'.format(account))
    if '.' not in account:
        blog_name += '.tumblr.com'
    return 'https://api.tumblr.com/v2/blog/%s/%s' % (
        blog_name, 'likes' if options.likes else 'posts'
    )


def add_exif(image_name, tags):
    assert pyexiv2 is not None
    try:
        metadata = pyexiv2.ImageMetadata(image_name)
        metadata.read()
    except OSError as e:
        logger.error('Error reading metadata for image {!r}: {!r}\n'.format(image_name, e))
        return
    KW_KEY = 'Iptc.Application2.Keywords'
    if '-' in options.exif:  # remove all tags
        if KW_KEY in metadata.iptc_keys:
            del metadata[KW_KEY]
    else:  # add tags
        if KW_KEY in metadata.iptc_keys:
            tags |= set(metadata[KW_KEY].value)
        tags = [tag.strip().lower() for tag in tags | options.exif if tag]
        metadata[KW_KEY] = pyexiv2.IptcTag(KW_KEY, tags)
    try:
        metadata.write()
    except OSError as e:
        logger.error('Writing metadata failed for tags {} in {!r}: {!r}\n'.format(tags, image_name, e))


def save_style():
    with open_text(BACKUP_CSS_FILENAME) as css:
        css.write('''\
@import url("override.css");

body { width: 720px; margin: 0 auto; }
body > footer { padding: 1em 0; }
header > img { float: right; }
img { max-width: 720px; }
blockquote { margin-left: 0; border-left: 8px #999 solid; padding: 0 24px; }
.archive h1, .subtitle, article { padding-bottom: 0.75em; border-bottom: 1px #ccc dotted; }
article[class^="liked-"] { background-color: #f0f0f8; }
.post a.llink { display: none; }
header a, footer a { text-decoration: none; }
footer, article footer a { font-size: small; color: #999; }
''')


def find_files(path, match=None):
    try:
        it = os.scandir(path)
    except FileNotFoundError:
        return  # ignore nonexistent dir
    with it:
        yield from (e.path for e in it if match is None or match(e.name))


def find_post_files():
    path = path_to(post_dir)
    if not options.dirs:
        yield from find_files(path, lambda n: n.endswith(post_ext))
        return

    indexes = (join(e, DIR_INDEX_FILENAME) for e in find_files(path))
    yield from filter(os.path.exists, indexes)


def match_avatar(name):
    return name.startswith(AVATAR_BASE + '.')


def get_avatar(prev_archive):
    if prev_archive is not None:
        # Copy old avatar, if present
        avatar_matches = find_files(join(prev_archive, THEME_DIR), match_avatar)
        src = next(avatar_matches, None)
        if src is not None:
            path_parts = (THEME_DIR, split(src)[-1])
            cpy_res = maybe_copy_media(prev_archive, path_parts)
            if cpy_res:
                return  # We got the avatar
    if options.no_get:
        return  # Don't download the avatar

    url = 'https://api.tumblr.com/v2/blog/%s/avatar' % blog_name
    avatar_dest = avatar_fpath = open_file(lambda f: f, (THEME_DIR, AVATAR_BASE))

    # Remove old avatars
    avatar_matches = find_files(THEME_DIR, match_avatar)
    if next(avatar_matches, None) is not None:
        return  # Do not clobber

    def adj_bn(old_bn, f):
        # Give it an extension
        image_type = guess_extension(f)
        if image_type:
            return avatar_fpath + '.' + image_type
        return avatar_fpath

    # Download the image
    assert wget_retrieve is not None
    try:
        wget_retrieve(url, avatar_dest, adjust_basename=adj_bn)
    except WGError as e:
        e.log()


def get_style(prev_archive):
    """Get the blog's CSS by brute-forcing it from the home page.
    The v2 API has no method for getting the style directly.
    See https://groups.google.com/d/msg/tumblr-api/f-rRH6gOb6w/sAXZIeYx5AUJ"""
    if prev_archive is not None:
        # Copy old style, if present
        path_parts = (THEME_DIR, 'style.css')
        cpy_res = maybe_copy_media(prev_archive, path_parts)
        if cpy_res:
            return  # We got the style
    if options.no_get:
        return  # Don't download the style

    url = 'https://%s/' % blog_name
    try:
        resp = urlopen(url, options)
        page_data = resp.data
    except HTTPError as e:
        logger.error('URL is {}\nError retrieving style: {}\n'.format(url, e))
        return
    for match in re.findall(br'(?s)<style type=.text/css.>(.*?)</style>', page_data):
        css = match.strip().decode('utf-8', errors='replace')
        if '\n' not in css:
            continue
        css = css.replace('\r', '').replace('\n    ', '\n')
        with open_text(THEME_DIR, 'style.css') as f:
            f.write(css + '\n')
        return


# Copy media file, if present in prev_archive
def maybe_copy_media(prev_archive, path_parts, pa_path_parts=None):
    if prev_archive is None:
        return False  # Source does not exist
    if pa_path_parts is None:
        pa_path_parts = path_parts  # Default

    srcpath = join(prev_archive, *pa_path_parts)
    dstpath = open_file(lambda f: f, path_parts)

    try:
        os.stat(srcpath)
    except FileNotFoundError:
        return False  # Source does not exist

    try:
        os.stat(dstpath)
    except FileNotFoundError:
        pass  # Destination does not exist yet
    else:
        return True  # Don't overwrite

    with open_outfile('wb', *path_parts) as dstf:
        copyfile(srcpath, dstf.name)
        shutil.copystat(srcpath, dstf.name)

    return True  # Copied


def check_optional_modules():
    if options.exif:
        if pyexiv2 is None:
            raise RuntimeError("--exif: module 'pyexiv2' is not installed")
        if not hasattr(pyexiv2, 'ImageMetadata'):
            raise RuntimeError("--exif: module 'pyexiv2' is missing features, perhaps you need 'py3exiv2'?")
    if options.filter is not None and jq is None:
        raise RuntimeError("--filter: module 'jq' is not installed")
    if options.save_notes or options.copy_notes:
        load_bs4('save notes' if options.save_notes else 'copy notes')
    if options.save_video and not (have_module('yt_dlp') or have_module('youtube_dl')):
        raise RuntimeError("--save-video: module 'youtube_dl' is not installed")



def import_youtube_dl():
    global ytdl_module
    if ytdl_module is not None:
        return ytdl_module

    try:
        import yt_dlp
    except ImportError:
        pass
    else:
        ytdl_module = yt_dlp
        return ytdl_module

    import youtube_dl

    ytdl_module = youtube_dl
    return ytdl_module


class Index:
    index: DefaultDict[int, DefaultDict[int, List['LocalPost']]]

    def __init__(self, blog, body_class='index'):
        self.blog = blog
        self.body_class = body_class
        self.index = defaultdict(lambda: defaultdict(list))

    def add_post(self, post):
        self.index[post.tm.tm_year][post.tm.tm_mon].append(post)

    def save_index(self, index_dir='.', title=None):
        archives = sorted(((y, m) for y in self.index for m in self.index[y]),
            reverse=options.reverse_month
        )
        subtitle = self.blog.title if title else self.blog.subtitle
        title = title or self.blog.title
        with open_text(index_dir, DIR_INDEX_FILENAME) as idx:
            idx.write(self.blog.header(title, self.body_class, subtitle, avatar=True))
            if options.tag_index and self.body_class == 'index':
                idx.write('<p><a href={}>Tag index</a></p>\n'.format(
                    urlpathjoin(TAG_INDEX_DIR, DIR_INDEX_FILENAME)
                ))
            for year in sorted(self.index.keys(), reverse=options.reverse_index):
                self.save_year(idx, archives, index_dir, year)
            idx.write('<footer><p>Generated on %s by <a href=https://github.com/'
                'bbolli/tumblr-utils>tumblr-utils</a>.</p></footer>\n' % strftime('%x %X')
            )

    def save_year(self, idx, archives, index_dir, year):
        idx.write('<h3>%s</h3>\n<ul>\n' % year)
        for month in sorted(self.index[year].keys(), reverse=options.reverse_index):
            tm = time.localtime(time.mktime((year, month, 3, 0, 0, 0, 0, 0, -1)))
            month_name = self.save_month(archives, index_dir, year, month, tm)
            idx.write('    <li><a href={} title="{} post(s)">{}</a></li>\n'.format(
                urlpathjoin(ARCHIVE_DIR, month_name), len(self.index[year][month]), strftime('%B', tm)
            ))
        idx.write('</ul>\n\n')

    def save_month(self, archives, index_dir, year, month, tm):
        posts = sorted(self.index[year][month], key=lambda x: x.date, reverse=options.reverse_month)
        posts_month = len(posts)
        posts_page = options.posts_per_page if options.posts_per_page >= 1 else posts_month

        def pages_per_month(y, m):
            posts_m = len(self.index[y][m])
            return posts_m // posts_page + bool(posts_m % posts_page)

        def next_month(inc):
            i = archives.index((year, month))
            i += inc
            if 0 <= i < len(archives):
                return archives[i]
            return 0, 0

        FILE_FMT = '%d-%02d-p%s%s'
        pages_month = pages_per_month(year, month)
        first_file: Optional[str] = None
        for page, start in enumerate(range(0, posts_month, posts_page), start=1):

            archive = [self.blog.header(strftime('%B %Y', tm), body_class='archive')]
            archive.extend(p.get_post(self.body_class == 'tag-archive') for p in posts[start:start + posts_page])

            suffix = '/' if options.dirs else post_ext
            file_name = FILE_FMT % (year, month, page, suffix)
            if options.dirs:
                base = urlpathjoin(save_dir, ARCHIVE_DIR)
                arch = open_text(index_dir, ARCHIVE_DIR, file_name, DIR_INDEX_FILENAME)
            else:
                base = ''
                arch = open_text(index_dir, ARCHIVE_DIR, file_name)

            if page > 1:
                pp = FILE_FMT % (year, month, page - 1, suffix)
            else:
                py, pm = next_month(-1)
                pp = FILE_FMT % (py, pm, pages_per_month(py, pm), suffix) if py else ''
                first_file = file_name

            if page < pages_month:
                np = FILE_FMT % (year, month, page + 1, suffix)
            else:
                ny, nm = next_month(+1)
                np = FILE_FMT % (ny, nm, 1, suffix) if ny else ''

            archive.append(self.blog.footer(base, pp, np))

            with arch as archf:
                archf.write('\n'.join(archive))

        assert first_file is not None
        return first_file


class TagIndex(Index):
    def __init__(self, blog, name):
        super().__init__(blog, 'tag-archive')
        self.name = name


class Indices:
    def __init__(self, blog):
        self.blog = blog
        self.main_index = Index(blog)
        self.tags = {}

    def build_index(self):
        for post in map(LocalPost, find_post_files()):
            self.main_index.add_post(post)
            if options.tag_index:
                for tag, name in post.tags:
                    if tag not in self.tags:
                        self.tags[tag] = TagIndex(self.blog, name)
                    self.tags[tag].name = name
                    self.tags[tag].add_post(post)

    def save_index(self):
        self.main_index.save_index()
        if options.tag_index:
            self.save_tag_index()

    def save_tag_index(self):
        global save_dir
        save_dir = '../../..'
        mkdir(path_to(TAG_INDEX_DIR))
        tag_index = [self.blog.header('Tag index', 'tag-index', self.blog.title, avatar=True), '<ul>']
        for tag, index in sorted(self.tags.items(), key=lambda kv: kv[1].name):
            digest = hashlib.md5(to_bytes(tag)).hexdigest()
            index.save_index(TAG_INDEX_DIR + os.sep + digest,
                "Tag ‛%s’" % index.name
            )
            tag_index.append('    <li><a href={}>{}</a></li>'.format(
                urlpathjoin(digest, DIR_INDEX_FILENAME), escape(index.name)
            ))
        tag_index.extend(['</ul>', ''])
        with open_text(TAG_INDEX_DIR, DIR_INDEX_FILENAME) as f:
            f.write('\n'.join(tag_index))


class TumblrBackup:
    def __init__(self):
        self.failed_blogs = []
        self.postfail_blogs = []
        self.total_count = 0
        self.post_count = 0
        self.filter_skipped = 0
        self.title: Optional[str] = None
        self.subtitle: Optional[str] = None
        self.pa_options: Optional[JSONDict] = None
        self.media_list_file: Optional[TextIO] = None
        self.mlf_seen: Set[int] = set()
        self.mlf_lock = threading.Lock()

    def exit_code(self):
        if self.failed_blogs or self.postfail_blogs:
            return EXIT_ERRORS
        if self.total_count == 0 and not options.json_info:
            return EXIT_NOPOSTS
        return EXIT_SUCCESS

    def header(self, title='', body_class='', subtitle='', avatar=False):
        root_rel = {
            'index': '', 'tag-index': '..', 'tag-archive': '../..'
        }.get(body_class, save_dir)
        css_rel = urlpathjoin(root_rel, CUSTOM_CSS_FILENAME if have_custom_css else BACKUP_CSS_FILENAME)
        if body_class:
            body_class = ' class=' + body_class
        h = '''<!DOCTYPE html>

<meta charset=%s>
<title>%s</title>
<link rel=stylesheet href=%s>

<body%s>

<header>
''' % (FILE_ENCODING, self.title, css_rel, body_class)
        if avatar:
            avatar_matches = find_files(path_to(THEME_DIR), match_avatar)
            avatar_path = next(avatar_matches, None)
            if avatar_path is not None:
                h += '<img src={} alt=Avatar>\n'.format(urlpathjoin(root_rel, THEME_DIR, split(avatar_path)[1]))
        if title:
            h += '<h1>%s</h1>\n' % title
        if subtitle:
            h += '<p class=subtitle>%s</p>\n' % subtitle
        h += '</header>\n'
        return h

    @staticmethod
    def footer(base, previous_page, next_page):
        f = '<footer><nav>'
        f += '<a href={} rel=index>Index</a>\n'.format(urlpathjoin(save_dir, DIR_INDEX_FILENAME))
        if previous_page:
            f += '| <a href={} rel=prev>Previous</a>\n'.format(urlpathjoin(base, previous_page))
        if next_page:
            f += '| <a href={} rel=next>Next</a>\n'.format(urlpathjoin(base, next_page))
        f += '</nav></footer>\n'
        return f

    @staticmethod
    def get_post_timestamp(post, BeautifulSoup_):
        if TYPE_CHECKING:
            from bs4 import BeautifulSoup
        else:
            BeautifulSoup = BeautifulSoup_

        with open(post, encoding=FILE_ENCODING) as pf:
            soup = BeautifulSoup(pf, 'lxml')
        postdate = cast(Tag, soup.find('time'))['datetime']
        # datetime.fromisoformat does not understand 'Z' suffix
        return int(datetime.strptime(cast(str, postdate), '%Y-%m-%dT%H:%M:%SZ').timestamp())

    @classmethod
    def process_existing_backup(cls, account, prev_archive):
        complete_backup = os.path.exists(path_to('.complete'))
        try:
            with open(path_to('.first_run_options'), encoding=FILE_ENCODING) as f:
                first_run_options = json.load(f)
        except FileNotFoundError:
            first_run_options = None

        class Options:
            def __init__(self, fro): self.fro = fro
            def differs(self, opt): return opt not in self.fro or orig_options[opt] != self.fro[opt]
            def first(self, opts): return {opt: self.fro.get(opt, '<not present>') for opt in opts}
            @staticmethod
            def this(opts): return {opt: orig_options[opt] for opt in opts}

        # These options must always match
        backdiff_nondef = None
        if first_run_options is not None:
            opts = Options(first_run_options)
            mustmatchdiff = tuple(filter(opts.differs, MUST_MATCH_OPTIONS))
            if mustmatchdiff:
                raise RuntimeError('{}: The script was given {} but the existing backup was made with {}'.format(
                    account, opts.this(mustmatchdiff), opts.first(mustmatchdiff)))

            backdiff = tuple(filter(opts.differs, BACKUP_CHANGING_OPTIONS))
            if complete_backup:
                # Complete archives may be added to with different options
                if (
                    options.resume
                    and first_run_options.get('count') is None
                    and (orig_options['period'] or [0, 0])[0] >= (first_run_options.get('period') or [0, 0])[0]
                ):
                    raise RuntimeError('{}: Cannot continue complete backup that was not stopped early with --count or '
                                       '--period'.format(account))
            elif options.resume:
                backdiff_nondef = tuple(opt for opt in backdiff if orig_options[opt] != parser.get_default(opt))
                if backdiff_nondef and not options.ignore_diffopt:
                    raise RuntimeError('{}: The script was given {} but the existing backup was made with {}. You may '
                                       'skip this check with --ignore-diffopt.'.format(
                                            account, opts.this(backdiff_nondef), opts.first(backdiff_nondef)))
            elif not backdiff:
                raise RuntimeError('{}: Found incomplete archive, try --continue'.format(account))
            elif not options.ignore_diffopt:
                raise RuntimeError('{}: Refusing to make a different backup (with {} instead of {}) over an incomplete '
                                   'archive. Delete the old backup to start fresh, or skip this check with '
                                   '--ignore-diffopt (optionally with --continue).'.format(
                                       account, opts.this(backdiff), opts.first(backdiff)))

        pa_options = None
        if prev_archive is not None:
            try:
                with open(join(prev_archive, '.first_run_options'), encoding=FILE_ENCODING) as f:
                    pa_options = json.load(f)
            except FileNotFoundError:
                pa_options = None

            # These options must always match
            if pa_options is not None:
                pa_opts = Options(pa_options)
                mustmatchdiff = tuple(filter(pa_opts.differs, PREV_MUST_MATCH_OPTIONS))
                if mustmatchdiff:
                    raise RuntimeError('{}: The script was given {} but the previous archive was made with {}'.format(
                        account, pa_opts.this(mustmatchdiff), pa_opts.first(mustmatchdiff)))

        oldest_tstamp = None
        if options.resume or not complete_backup:
            # Read every post to find the oldest timestamp already saved
            post_glob = list(find_post_files())
            if not options.resume:
                pass  # No timestamp needed but may want to know if posts are present
            elif not post_glob:
                raise RuntimeError('{}: Cannot continue empty backup'.format(account))
            else:
                logger.warn('Found incomplete backup.\n', account=True)
                BeautifulSoup = load_bs4('continue incomplete backup')
                if options.likes:
                    logger.warn('Finding oldest liked post (may take a while)\n', account=True)
                    oldest_tstamp = min(cls.get_post_timestamp(post, BeautifulSoup) for post in post_glob)
                else:
                    post_min = min(post_glob, key=lambda f: int(splitext(split(f)[1])[0]))
                    oldest_tstamp = cls.get_post_timestamp(post_min, BeautifulSoup)
                logger.info(
                    'Backing up posts before timestamp={} ({})\n'.format(oldest_tstamp, time.ctime(oldest_tstamp)),
                    account=True,
                )

        write_fro = False
        if backdiff_nondef is not None:
            # Load saved options, unless they were overridden with --ignore-diffopt
            for opt in BACKUP_CHANGING_OPTIONS:
                if opt not in backdiff_nondef:
                    setattr(options, opt, first_run_options[opt])
        else:
            # Load original options
            for opt in BACKUP_CHANGING_OPTIONS:
                setattr(options, opt, orig_options[opt])
            if first_run_options is None and not (complete_backup or post_glob):
                # Presumably this is the initial backup of this blog
                write_fro = True

        if pa_options is None and prev_archive is not None:
            # Fallback assumptions
            logger.warn('Warning: Unknown media path options for previous archive, assuming they match ours\n',
                        account=True)
            pa_options = {opt: getattr(options, opt) for opt in MEDIA_PATH_OPTIONS}

        return oldest_tstamp, pa_options, write_fro

    def record_media(self, ident: int, urls: Set[str]) -> None:
        with self.mlf_lock:
            if self.media_list_file is not None and ident not in self.mlf_seen:
                json.dump(dict(post=ident, media=sorted(urls)), self.media_list_file, separators=(',', ':'))
                self.media_list_file.write('\n')
                self.mlf_seen.add(ident)

    def backup(self, account, prev_archive):
        """makes single files and an index for every post on a public Tumblr blog account"""

        base = get_api_url(account)

        # make sure there are folders to save in
        global save_folder, media_folder, post_ext, post_dir, save_dir, have_custom_css
        if options.json_info:
            pass  # Not going to save anything
        elif options.blosxom:
            save_folder = ROOT_FOLDER
            post_ext = '.txt'
            post_dir = os.curdir
            post_class: Type[TumblrPost] = BlosxomPost
        else:
            save_folder = join(ROOT_FOLDER, options.outdir or account)
            media_folder = path_to(media_dir)
            if options.dirs:
                post_ext = ''
                save_dir = '../..'
            post_class = TumblrPost
            have_custom_css = os.access(path_to(CUSTOM_CSS_FILENAME), os.R_OK)

        self.post_count = 0
        self.filter_skipped = 0

        oldest_tstamp, self.pa_options, write_fro = self.process_existing_backup(account, prev_archive)
        check_optional_modules()

        if options.idents:
            # Normalize idents
            options.idents.sort(reverse=True)

        if options.incremental or options.resume:
            post_glob = list(find_post_files())

        ident_max = None
        if options.incremental and post_glob:
            if options.likes:
                # Read every post to find the newest timestamp already saved
                logger.warn('Finding newest liked post (may take a while)\n', account=True)
                BeautifulSoup = load_bs4('backup likes incrementally')
                ident_max = max(self.get_post_timestamp(post, BeautifulSoup) for post in post_glob)
                logger.info('Backing up posts after timestamp={} ({})\n'.format(ident_max, time.ctime(ident_max)),
                            account=True)
            else:
                # Get the highest post id already saved
                ident_max = max(int(splitext(split(f)[1])[0]) for f in post_glob)
                logger.info('Backing up posts after id={}\n'.format(ident_max), account=True)

        if options.resume:
            # Update skip and count based on where we left off
            options.skip = 0
            self.post_count = len(post_glob)

        logger.status('Getting basic information\r')

        api_parser = ApiParser(base, account)
        if not api_parser.read_archive(prev_archive):
            self.failed_blogs.append(account)
            return
        resp = api_parser.get_initial()
        if not resp:
            self.failed_blogs.append(account)
            return

        # collect all the meta information
        if options.likes:
            if not resp.get('blog', {}).get('share_likes', True):
                logger.error('{} does not have public likes\n'.format(account))
                self.failed_blogs.append(account)
                return
            posts_key = 'liked_posts'
            blog = {}
            count_estimate = resp['liked_count']
        else:
            posts_key = 'posts'
            blog = resp.get('blog', {})
            count_estimate = blog.get('posts')
        self.title = escape(blog.get('title', account))
        self.subtitle = blog.get('description', '')

        if options.json_info:
            posts = resp[posts_key]
            info = {'uuid': blog.get('uuid'),
                    'post_count': count_estimate,
                    'last_post_ts': posts[0]['timestamp'] if posts else None}
            json.dump(info, sys.stdout)
            return

        if write_fro:
            # Blog directory gets created here
            with open_text('.first_run_options') as f:
                f.write(json.dumps(orig_options))

        def build_index():
            logger.status('Getting avatar and style\r')
            get_avatar(prev_archive)
            get_style(prev_archive)
            if not have_custom_css:
                save_style()
            logger.status('Building index\r')
            ix = Indices(self)
            ix.build_index()
            ix.save_index()

            if not (account in self.failed_blogs or os.path.exists(path_to('.complete'))):
                # Make .complete file
                sf: Optional[int]
                if os.name == 'posix':  # Opening directories and fdatasync are POSIX features
                    sf = opendir(save_folder, os.O_RDONLY)
                else:
                    sf = None
                try:
                    if sf is not None:
                        fdatasync(sf)
                    with open(open_file(lambda f: f, ('.complete',)), 'wb') as f:
                        fsync(f)
                    if sf is not None:
                        fdatasync(sf)
                finally:
                    if sf is not None:
                        os.close(sf)

        if not options.blosxom and options.count == 0:
            build_index()
            return

        # use the meta information to create a HTML header
        TumblrPost.post_header = self.header(body_class='post')

        jq_filter = request_sets = None
        if options.filter is not None:
            assert jq is not None
            jq_filter = jq.compile(options.filter)
        if options.request is not None:
            request_sets = {typ: set(tags) for typ, tags in options.request.items()}

        # start the thread pool
        backup_pool = ThreadPool()

        before = options.period[1] if options.period else None
        if oldest_tstamp is not None:
            before = oldest_tstamp if before is None else min(before, oldest_tstamp)
        if before is not None and api_parser.dashboard_only_blog:
            logger.warn('Warning: skipping posts on a dashboard-only blog is slow\n', account=True)

        # returns whether any posts from this batch were saved
        def _backup(posts):
            def sort_key(x): return x['liked_timestamp'] if options.likes else int(x['id'])
            oldest_date = None
            for p in sorted(posts, key=sort_key, reverse=True):
                no_internet.check()
                enospc.check()
                post = post_class(p, account, prev_archive, self.pa_options, self.record_media)
                oldest_date = post.date
                if before is not None and post.date >= before:
                    if api_parser.dashboard_only_blog:
                        continue  # cannot request 'before' with the svc API
                    raise RuntimeError('Found post with date ({}) newer than before param ({})'.format(
                        post.date, before))
                if ident_max is None:
                    pass  # No limit
                elif (p['liked_timestamp'] if options.likes else int(post.ident)) <= ident_max:
                    logger.info('Stopping backup: Incremental backup complete\n', account=True)
                    return False, oldest_date
                if options.period and post.date < options.period[0]:
                    logger.info('Stopping backup: Reached end of period\n', account=True)
                    return False, oldest_date
                if next_ident is not None and int(post.ident) != next_ident:
                    logger.error("post '{}' not found\n".format(next_ident), account=True)
                    return False, oldest_date
                if request_sets:
                    if post.typ not in request_sets:
                        continue
                    tags = request_sets[post.typ]
                    if not (TAG_ANY in tags or tags & {t.lower() for t in post.tags}):
                        continue
                if options.no_reblog and post_is_reblog(p):
                    continue
                if options.only_reblog and not post_is_reblog(p):
                    continue
                if jq_filter:
                    try:
                        matches = jq_filter.input(p).first()
                    except StopIteration:
                        matches = False
                    if not matches:
                        self.filter_skipped += 1
                        continue
                if os.path.exists(path_to(*post.get_path())) and options.no_post_clobber:
                    continue  # Post exists and no-clobber enabled

                with multicond:
                    while backup_pool.queue.qsize() >= backup_pool.queue.maxsize:
                        no_internet.check(release=True)
                        enospc.check(release=True)
                        # All conditions false, wait for a change
                        multicond.wait((backup_pool.queue.not_full, no_internet.cond, enospc.cond))
                    backup_pool.add_work(post.save_post)

                self.post_count += 1
                if options.count and self.post_count >= options.count:
                    logger.info('Stopping backup: Reached limit of {} posts\n'.format(options.count), account=True)
                    return False, oldest_date
            return True, oldest_date

        api_thread = AsyncCallable(main_thread_lock, api_parser.apiparse, 'API Thread')

        next_ident: Optional[int] = None
        if options.idents is not None:
            remaining_idents = options.idents.copy()
            count_estimate = len(remaining_idents)

        if options.media_list:
            mlf = open_text('media.json', mode='r+')
            self.media_list_file = mlf.__enter__()
            self.mlf_seen.clear()
            for line in self.media_list_file:
                doc = json.loads(line)
                self.mlf_seen.add(doc['post'])
        else:
            mlf = None

        try:
            # Get the JSON entries from the API, which we can only do for MAX_POSTS posts at once.
            # Posts "arrive" in reverse chronological order. Post #0 is the most recent one.
            i = options.skip

            while True:
                # find the upper bound
                logger.status('Getting {}posts {} to {}{}\r'.format(
                    'liked ' if options.likes else '', i, i + MAX_POSTS - 1,
                    '' if count_estimate is None else ' (of {} expected)'.format(count_estimate),
                ))

                if options.idents is not None:
                    try:
                        next_ident = remaining_idents.pop(0)
                    except IndexError:
                        # if the last requested post does not get backed up we end up here
                        logger.info('Stopping backup: End of requested posts\n', account=True)
                        break

                with multicond:
                    api_thread.put(MAX_POSTS, i, before, next_ident)

                    while not api_thread.response.qsize():
                        no_internet.check(release=True)
                        enospc.check(release=True)
                        # All conditions false, wait for a change
                        multicond.wait((api_thread.response.not_empty, no_internet.cond, enospc.cond))

                    resp = api_thread.get(block=False)

                if resp is None:
                    self.failed_blogs.append(account)
                    break

                posts = resp[posts_key]
                if not posts:
                    logger.info('Backup complete: Found empty set of posts\n', account=True)
                    break

                res, oldest_date = _backup(posts)
                if not res:
                    break

                if options.likes:
                    next_ = resp['_links'].get('next')
                    if next_ is None:
                        logger.info('Backup complete: Found end of likes\n', account=True)
                        break
                    before = int(next_['query_params']['before'])
                elif before is not None and not api_parser.dashboard_only_blog:
                    assert oldest_date <= before
                    if oldest_date == before:
                        oldest_date -= 1
                    before = oldest_date

                if options.idents is None:
                    i += MAX_POSTS
                else:
                    i += 1

            api_thread.quit()
            backup_pool.wait()  # wait until all posts have been saved
        except:
            api_thread.quit()
            backup_pool.cancel()  # ensure proper thread pool termination
            raise
        finally:
            if mlf is not None:
                mlf.__exit__(*sys.exc_info())
                self.media_list_file = None

        if backup_pool.errors:
            self.postfail_blogs.append(account)

        # postprocessing
        if not options.blosxom and self.post_count:
            build_index()

        logger.status(None)
        skipped_msg = (', {} did not match filter'.format(self.filter_skipped)) if self.filter_skipped else ''
        logger.warn(
            '{} {}posts backed up{}\n'.format(self.post_count, 'liked ' if options.likes else '', skipped_msg),
            account=True,
        )
        self.total_count += self.post_count


if __name__ == '__main__':
    # The default of 'fork' can cause deadlocks, even on Linux
    # See https://bugs.python.org/issue40399
    if 'forkserver' in multiprocessing.get_all_start_methods():
        multiprocessing.set_start_method('forkserver')  # Fastest safe option, if supported
    else:
        multiprocessing.set_start_method('spawn')  # Slow but safe

    # Raises SystemExit to terminate gracefully
    def handle_term_signal(signum, frame):
        if sys.is_finalizing():
            return  # Not a good time to exit
        sys.exit(1)
    signal.signal(signal.SIGTERM, handle_term_signal)
    if hasattr(signal, 'SIGHUP'):
        signal.signal(signal.SIGHUP, handle_term_signal)

    no_internet.setup(main_thread_lock)
    enospc.setup(main_thread_lock)

    options = parser.parse_args()
    blogs = options.blogs or DEFAULT_BLOGS
    del options.blogs

    if not blogs:
        parser.error('Missing blog-name')
    if options.auto is not None and options.auto != time.localtime().tm_hour:
        options.incremental = True
    if options.resume or options.incremental:
        # Do not clobber or count posts that were already backed up
        options.no_post_clobber = True
    if options.json_info:
        options.quiet = True
    if options.count is not None and options.count < 0:
        parser.error('--count: count must not be negative')
    if options.count == 0 and (options.incremental or options.auto is not None):
        parser.error('--count 0 conflicts with --incremental and --auto')
    if options.skip < 0:
        parser.error('--skip: skip must not be negative')
    if options.posts_per_page < 0:
        parser.error('--posts-per-page: posts per page must not be negative')
    if options.outdir and len(blogs) > 1:
        parser.error("-O can only be used for a single blog-name")
    if options.dirs and options.tag_index:
        parser.error("-D cannot be used with --tag-index")
    if options.cookiefile is not None and not os.access(options.cookiefile, os.R_OK):
        parser.error('--cookiefile: file cannot be read')
    if options.notes_limit is not None:
        if not options.save_notes:
            parser.error('--notes-limit requires --save-notes')
        if options.notes_limit < 1:
            parser.error('--notes-limit: Value must be at least 1')
    if options.prev_archives and options.reuse_json:
        parser.error('--prev-archives and --reuse-json are mutually exclusive')
    if options.prev_archives:
        if len(options.prev_archives) != len(blogs):
            parser.error('--prev-archives: expected {} directories, got {}'.format(
                len(blogs), len(options.prev_archives),
            ))
        for blog, pa in zip(blogs, options.prev_archives):
            if not os.access(pa, os.R_OK | os.X_OK):
                parser.error("--prev-archives: directory '{}' cannot be read".format(pa))
            blogdir = os.curdir if options.blosxom else (options.outdir or blog)
            if os.path.realpath(pa) == os.path.realpath(blogdir):
                parser.error("--prev-archives: Directory '{}' is also being written to. Use --reuse-json instead if "
                             "you want this, or specify --outdir if you don't.".format(pa))
    if options.threads < 1:
        parser.error('--threads: must use at least one thread')
    if options.no_get and not (options.prev_archives or options.reuse_json):
        parser.error('--no-get makes no sense without --prev-archives or --reuse-json')
    if options.no_get and options.save_notes:
        logger.warn('Warning: --save-notes uses HTTP regardless of --no-get\n')
    if options.copy_notes and not (options.prev_archives or options.reuse_json):
        parser.error('--copy-notes requires --prev-archives or --reuse-json')
    if options.idents is not None and options.likes:
        parser.error('--id-file not implemented for likes')
    if options.copy_notes is None:
        # Default to True if we may regenerate posts
        options.copy_notes = options.reuse_json and not (options.no_post_clobber or options.mtime_fix)

    # NB: this is done after setting implied options
    orig_options = vars(options).copy()

    check_optional_modules()

    if not API_KEY:
        sys.stderr.write('''\
Missing API_KEY; please get your own API key at
https://www.tumblr.com/oauth/apps\n''')
        sys.exit(1)

    wget_retrieve = WgetRetrieveWrapper(options, logger.log)
    setup_wget(not options.no_ssl_verify, options.user_agent)

    ApiParser.setup()
    tb = TumblrBackup()
    try:
        for i, account in enumerate(blogs):
            logger.backup_account = account
            tb.backup(account, options.prev_archives[i] if options.prev_archives else None)
    except KeyboardInterrupt:
        sys.exit(EXIT_INTERRUPT)

    if tb.failed_blogs:
        logger.warn('Failed to back up {}\n'.format(', '.join(tb.failed_blogs)))
    if tb.postfail_blogs:
        logger.warn('One or more posts failed to save for {}\n'.format(', '.join(tb.postfail_blogs)))
    sys.exit(tb.exit_code())
