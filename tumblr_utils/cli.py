from typing import Any, Callable

import argparse
import calendar
import re
import time

from tumblr_utils.constants import POST_TYPES, TAG_ANY, TYPE_ANY


def parse_period_date(period):
    """Prepare the period start and end timestamps"""
    timefn: Callable[[Any], float] = time.mktime
    # UTC marker
    if period[-1] == 'Z':
        period = period[:-1]
        timefn = calendar.timegm

    i = 0
    tm = [int(period[:4]), 1, 1, 0, 0, 0, 0, 0, -1]
    if len(period) >= 6:
        i = 1
        tm[1] = int(period[4:6])
    if len(period) == 8:
        i = 2
        tm[2] = int(period[6:8])

    def mktime(tml):
        tmt: Any = tuple(tml)
        return timefn(tmt)

    p_start = int(mktime(tm))
    tm[i] += 1
    p_stop = int(mktime(tm))
    return [p_start, p_stop]


class PeriodCallback(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        try:
            pformat = {'y': '%Y', 'm': '%Y%m', 'd': '%Y%m%d'}[values]
        except KeyError:
            periods = values.replace('-', '').split(',')
            if not all(re.match(r'\d{4}(\d\d)?(\d\d)?Z?$', p) for p in periods):
                parser.error("Period must be 'y', 'm', 'd' or YYYY[MM[DD]][Z]")
            if not (1 <= len(periods) < 3):
                parser.error('Period must have either one year/month/day or a start and end')
            prange = parse_period_date(periods.pop(0))
            if periods:
                prange[1] = parse_period_date(periods.pop(0))[0]
        else:
            period = time.strftime(pformat)
            prange = parse_period_date(period)
        setattr(namespace, self.dest, prange)


class CSVCallback(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, list(values.split(',')))


class RequestCallback(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        request = getattr(namespace, self.dest) or {}
        for req in values.lower().split(','):
            parts = req.strip().split(':')
            typ = parts.pop(0)
            if typ != TYPE_ANY and typ not in POST_TYPES:
                parser.error("{}: invalid post type '{}'".format(option_string, typ))
            for typ in POST_TYPES if typ == TYPE_ANY else (typ,):
                if not parts:
                    request[typ] = [TAG_ANY]
                    continue
                if typ not in request:
                    request[typ] = []
                request[typ].extend(parts)
        setattr(namespace, self.dest, request)


class TagsCallback(RequestCallback):
    def __call__(self, parser, namespace, values, option_string=None):
        super().__call__(
            parser, namespace, TYPE_ANY + ':' + values.replace(',', ':'), option_string,
        )


class IdFileCallback(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        with open(values) as f:
            setattr(namespace, self.dest, sorted(
                map(int, (line for line in map(lambda l: l.rstrip('\n'), f) if line)),
                reverse=True,
            ))


parser = argparse.ArgumentParser(usage='%(prog)s [options] blog-name ...',
                                 description='Makes a local backup of Tumblr blogs.')

parser.add_argument('-O', '--outdir', help='set the output directory (default: blog-name)')
parser.add_argument('-D', '--dirs', action='store_true', help='save each post in its own folder')
parser.add_argument('-q', '--quiet', action='store_true', help='suppress progress messages')
parser.add_argument('-l', '--likes', action='store_true', help="save a blog's likes, not its posts")
parser.add_argument('-k', '--skip-images', action='store_false', dest='save_images',
                    help='do not save images; link to Tumblr instead')
parser.add_argument('--save-video', action='store_true', help='save all video files')
parser.add_argument('--save-video-tumblr', action='store_true', help='save only Tumblr video files')
parser.add_argument('--save-audio', action='store_true', help='save audio files')
parser.add_argument('--save-notes', action='store_true', help='save a list of notes for each post')
parser.add_argument('--copy-notes', action='store_true', default=None,
                    help='copy the notes list from a previous archive (inverse: --no-copy-notes)')
parser.add_argument('--no-copy-notes', action='store_false', default=None, dest='copy_notes',
                    help=argparse.SUPPRESS)
parser.add_argument('--notes-limit', type=int, metavar='COUNT', help='limit requested notes to COUNT, per-post')
parser.add_argument('--cookiefile', help='cookie file for youtube-dl, --save-notes, and svc API')
parser.add_argument('-j', '--json', action='store_true', help='save the original JSON source')
parser.add_argument('-b', '--blosxom', action='store_true', help='save the posts in blosxom format')
parser.add_argument('-r', '--reverse-month', action='store_false',
                    help='reverse the post order in the monthly archives')
parser.add_argument('-R', '--reverse-index', action='store_false', help='reverse the index file order')
parser.add_argument('--tag-index', action='store_true', help='also create an archive per tag')
parser.add_argument('-n', '--count', type=int, help='save only COUNT posts')
parser.add_argument('-s', '--skip', type=int, default=0, help='skip the first SKIP posts')
parser.add_argument('-p', '--period', action=PeriodCallback,
                    help="limit the backup to PERIOD ('y', 'm', 'd', YYYY[MM[DD]][Z], or START,END)")
parser.add_argument('-N', '--posts-per-page', type=int, default=50, metavar='COUNT',
                    help='set the number of posts per monthly page, 0 for unlimited')
parser.add_argument('-Q', '--request', action=RequestCallback,
                    help='save posts matching the request TYPE:TAG:TAG:…,TYPE:TAG:…,…. '
                    'TYPE can be {} or {any}; TAGs can be omitted or a colon-separated list. '
                    'Example: -Q {any}:personal,quote,photo:me:self'
                    .format(', '.join(POST_TYPES), any=TYPE_ANY))
parser.add_argument('-t', '--tags', action=TagsCallback, dest='request',
                    help='save only posts tagged TAGS (comma-separated values; case-insensitive)')
parser.add_argument('-T', '--type', action=RequestCallback, dest='request',
                    help='save only posts of type TYPE (comma-separated values from {})'
                    .format(', '.join(POST_TYPES)))
parser.add_argument('-F', '--filter', help='save posts matching a jq filter (needs jq module)')
parser.add_argument('-I', '--image-names', choices=('o', 'i', 'bi'), default='o', metavar='FMT',
                    help="image filename format ('o'=original, 'i'=<post-id>, 'bi'=<blog-name>_<post-id>)")
parser.add_argument('-e', '--exif', action=CSVCallback, default=[], metavar='KW',
                    help='add EXIF keyword tags to each picture'
                    " (comma-separated values; '-' to remove all tags, '' to add no extra tags)")
parser.add_argument('-S', '--no-ssl-verify', action='store_true', help='ignore SSL verification errors')
parser.add_argument('--prev-archives', action=CSVCallback, default=[], metavar='DIRS',
                    help='comma-separated list of directories (one per blog) containing previous blog archives')
parser.add_argument('--no-post-clobber', action='store_true', help='Do not re-download existing posts')
parser.add_argument('--no-server-timestamps', action='store_false', dest='use_server_timestamps',
                    help="don't set local timestamps from HTTP headers")
parser.add_argument('--hostdirs', action='store_true', help='Generate host-prefixed directories for media')
parser.add_argument('--user-agent', help='User agent string to use with HTTP requests')
parser.add_argument('--skip-dns-check', action='store_false', dest='use_dns_check',
                    help='Skip DNS checks for internet access')
parser.add_argument('--threads', type=int, default=20, help='number of threads to use for post retrieval')
parser.add_argument('--ignore-diffopt', action='store_true',
                    help='Force backup over an incomplete archive with different options')
parser.add_argument('--no-get', action='store_true', help="Don't retrieve files not found in --prev-archives")
parser.add_argument('--internet-archive', action='store_true',
                    help='Fall back to the Internet Archive for Tumblr media 403 and 404 responses')
parser.add_argument('--media-list', action='store_true', help='Save post media URLs to media.json')
parser.add_argument('--id-file', action=IdFileCallback, dest='idents', metavar='FILE',
                    help='file containing a list of post IDs to save, one per line')
parser.add_argument('--json-info', action='store_true',
                    help="Just print some info for each blog, don't make a backup")
parser.add_argument('blogs', nargs='*')

postexist_group = parser.add_mutually_exclusive_group()
postexist_group.add_argument('-i', '--incremental', action='store_true', help='incremental backup mode')
postexist_group.add_argument('-a', '--auto', type=int, metavar='HOUR',
                             help='do a full backup at HOUR hours, otherwise do an incremental backup'
                             ' (useful for cron jobs)')
postexist_group.add_argument('--reuse-json', action='store_true',
                             help='Reuse the API responses saved with --json (implies --copy-notes)')
postexist_group.add_argument('--continue', action='store_true', dest='resume',
                             help='Continue an incomplete first backup')

reblog_group = parser.add_mutually_exclusive_group()
reblog_group.add_argument('--no-reblog', action='store_true', help="don't save reblogged posts")
reblog_group.add_argument('--only-reblog', action='store_true', help='save only reblogged posts')
