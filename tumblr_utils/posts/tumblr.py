from typing import Any, Callable, Literal, Optional, Set, Tag, Tuple, TYPE_CHECKING, Union

import json
import multiprocessing
import os
import re
import time

from datetime import datetime
from posixpath import basename as urlbasename, join as posix_path_join, urlsplitext as posix_split_ext
from urllib.parse import quote as urlquote, urlparse
from xml.sax.saxutils import escape as xml_escape

from tumblr_utils.constants import DIR_INDEX_FILENAME, JSON_DIR, JSONDict, TAGLINK_FMT, TAG_FMT
from tumblr_utils.utils import file_path_to, strftime
from tumblr_utils.utils.wget import touch


class TumblrPost:
    post_header = ''  # set by TumblrBackup.backup()

    def __init__(
        self,
        post: JSONDict,
        backup_account: str,
        prev_archive: Optional[str],
        pa_options: Optional[JSONDict],
        record_media: Callable[[int, Set[str]], None],
    ) -> None:
        self.post = post
        self.backup_account = backup_account
        self.prev_archive = prev_archive
        self.pa_options = pa_options
        self.record_media = record_media
        self.post_media: Set[str] = set()
        self.creator = post.get('blog_name') or post['tumblelog']
        self.ident = str(post['id'])
        self.url = post['post_url']
        self.shorturl = post['short_url']
        self.typ = str(post['type'])
        self.date: float = post['liked_timestamp' if options.likes else 'timestamp']
        self.isodate = datetime.utcfromtimestamp(self.date).isoformat() + 'Z'
        self.tm = time.localtime(self.date)
        self.title = ''
        self.tags: str = post['tags']
        self.note_count = post.get('note_count')
        if self.note_count is None:
            self.note_count = post.get('notes', {}).get('count')
        if self.note_count is None:
            self.note_count = 0
        self.reblogged_from = post.get('reblogged_from_url')
        self.reblogged_root = post.get('reblogged_root_url')
        self.source_title = post.get('source_title', '')
        self.source_url = post.get('source_url', '')
        self.file_name = os.path.join(self.ident, DIR_INDEX_FILENAME) if options.dirs else self.ident + post_ext
        self.llink = self.ident if options.dirs else self.file_name
        self.media_dir = os.path.join(post_dir, self.ident) if options.dirs else media_dir
        self.media_url = posix_path_join(save_dir, self.media_dir)
        self.media_folder = file_path_to(self.media_dir)

    def get_content(self):
        """generates the content for this post"""
        post = self.post
        content = []
        self.post_media.clear()

        def append(s, fmt='%s'):
            content.append(fmt % s)

        def get_try(elt) -> Union[Any, Literal['']]:
            return post.get(elt, '')

        def append_try(elt, fmt='%s'):
            elt = get_try(elt)
            if elt:
                if options.save_images:
                    elt = re.sub(r'''(?i)(<img\s(?:[^>]*\s)?src\s*=\s*["'])(.*?)(["'][^>]*>)''',
                                 self.get_inline_image, elt
                                 )
                if options.save_video or options.save_video_tumblr:
                    # Handle video element poster attribute
                    elt = re.sub(r'''(?i)(<video\s(?:[^>]*\s)?poster\s*=\s*["'])(.*?)(["'][^>]*>)''',
                                 self.get_inline_video_poster, elt
                                 )
                    # Handle video element's source sub-element's src attribute
                    elt = re.sub(r'''(?i)(<source\s(?:[^>]*\s)?src\s*=\s*["'])(.*?)(["'][^>]*>)''',
                                 self.get_inline_video, elt
                                 )
                append(elt, fmt)

        if self.typ == 'text':
            self.title = get_try('title')
            append_try('body')

        elif self.typ == 'photo':
            url = get_try('link_url')
            is_photoset = len(post['photos']) > 1
            for offset, p in enumerate(post['photos'], start=1):
                o = p['alt_sizes'][0] if 'alt_sizes' in p else p['original_size']
                src = o['url']
                if options.save_images:
                    src = self.get_image_url(src, offset if is_photoset else 0)
                append(xml_escape(src), '<img alt="" src="%s">')
                if url:
                    content[-1] = '<a href="%s">%s</a>' % (xml_escape(url), content[-1])
                content[-1] = '<p>' + content[-1] + '</p>'
                if p['caption']:
                    append(p['caption'], '<p>%s</p>')
            append_try('caption')

        elif self.typ == 'link':
            url = post['url']
            self.title = '<a href="%s">%s</a>' % (xml_escape(url), post['title'] or url)
            append_try('description')

        elif self.typ == 'quote':
            append(post['text'], '<blockquote><p>%s</p></blockquote>')
            append_try('source', '<p>%s</p>')

        elif self.typ == 'video':
            src = ''
            if (options.save_video or options.save_video_tumblr) \
                    and post['video_type'] == 'tumblr':
                src = self.get_media_url(post['video_url'], '.mp4')
            elif options.save_video:
                src = self.get_youtube_url(self.url)
                if not src:
                    logger.warn('Unable to download video in post #{}\n'.format(self.ident))
            if src:
                append('<p><video controls><source src="%s" type=video/mp4>%s<br>\n<a href="%s">%s</a></video></p>' % (
                    src, "Your browser does not support the video element.", src, "Video file"
                ))
            else:
                player = get_try('player')
                if player:
                    append(player[-1]['embed_code'])
                else:
                    append_try('video_url')
            append_try('caption')

        elif self.typ == 'audio':
            def make_player(src_):
                append('<p><audio controls><source src="{src}" type=audio/mpeg>{}<br>\n<a href="{src}">{}'
                       '</a></audio></p>'
                       .format('Your browser does not support the audio element.', 'Audio file', src=src_))

            src = None
            audio_url = get_try('audio_url') or get_try('audio_source_url')
            if options.save_audio:
                if post['audio_type'] == 'tumblr':
                    if audio_url.startswith('https://a.tumblr.com/'):
                        src = self.get_media_url(audio_url, '.mp3')
                    elif audio_url.startswith('https://www.tumblr.com/audio_file/'):
                        audio_url = 'https://a.tumblr.com/{}o1.mp3'.format(urlbasename(urlparse(audio_url).path))
                        src = self.get_media_url(audio_url, '.mp3')
                elif post['audio_type'] == 'soundcloud':
                    src = self.get_media_url(audio_url, '.mp3')
            player = get_try('player')
            if src:
                make_player(src)
            elif player:
                append(player)
            elif audio_url:
                make_player(audio_url)
            append_try('caption')

        elif self.typ == 'answer':
            self.title = post['question']
            append_try('answer')

        elif self.typ == 'chat':
            self.title = get_try('title')
            append(
                '<br>\n'.join('%(label)s %(phrase)s' % d for d in post['dialogue']),
                '<p>%s</p>'
            )

        else:
            logger.warn("Unknown post type '{}' in post #{}\n".format(self.typ, self.ident))
            append(xml_escape(self.get_json_content()), '<pre>%s</pre>')

        # Write URLs to media.json
        self.record_media(int(self.ident), self.post_media)

        content_str = '\n'.join(content)

        # fix wrongly nested HTML elements
        for p in ('<p>(<({})>)', '(</({})>)</p>'):
            content_str = re.sub(p.format('p|ol|iframe[^>]*'), r'\1', content_str)

        return content_str

    def get_youtube_url(self, youtube_url):
        # determine the media file name
        filetmpl = '%(id)s_%(uploader_id)s_%(title)s.%(ext)s'
        ydl_options = {
            'outtmpl': os.path.join(self.media_folder, filetmpl),
            'quiet': True,
            'restrictfilenames': True,
            'noplaylist': True,
            'continuedl': True,
            'nooverwrites': True,
            'retries': 3000,
            'fragment_retries': 3000,
            'ignoreerrors': True,
        }
        if options.cookiefile is not None:
            ydl_options['cookiefile'] = options.cookiefile

        if TYPE_CHECKING:
            import youtube_dl
        else:
            youtube_dl = import_youtube_dl()

        ydl = youtube_dl.YoutubeDL(ydl_options)
        ydl.add_default_info_extractors()
        try:
            result = ydl.extract_info(youtube_url, download=False)
            media_filename = youtube_dl.utils.sanitize_filename(filetmpl % result['entries'][0], restricted=True)
        except Exception:
            return ''

        # check if a file with this name already exists
        if not os.path.isfile(media_filename):
            try:
                ydl.extract_info(youtube_url, download=True)
            except Exception:
                return ''
        return posix_path_join(self.media_url, os.path.split(media_filename)[1])

    def get_media_url(self, media_url, extension):
        if not media_url:
            return ''
        saved_name = self.download_media(media_url, extension=extension)
        if saved_name is not None:
            return posix_path_join(self.media_url, saved_name)
        return media_url

    def get_image_url(self, image_url, offset):
        """Saves an image if not saved yet. Returns the new URL or
        the original URL in case of download errors."""
        saved_name = self.download_media(image_url, offset='_o%s' % offset if offset else '')
        if saved_name is not None:
            if options.exif and saved_name.endswith('.jpg'):
                add_exif(os.path.join(self.media_folder, saved_name), set(self.tags))
            return posix_path_join(self.media_url, saved_name)
        return image_url

    @staticmethod
    def maxsize_image_url(image_url):
        if ".tumblr.com/" not in image_url or image_url.endswith('.gif'):
            return image_url
        # change the image resolution to 1280
        return re.sub(r'_\d{2,4}(\.\w+)$', r'_1280\1', image_url)

    def get_inline_image(self, match):
        """Saves an inline image if not saved yet. Returns the new <img> tag or
        the original one in case of download errors."""
        image_url, image_filename = self._parse_url_match(match, transform=self.maxsize_image_url)
        if not image_filename or not image_url.startswith('http'):
            return match.group(0)
        saved_name = self.download_media(image_url, filename=image_filename)
        if saved_name is None:
            return match.group(0)
        return '%s%s/%s%s' % (match.group(1), self.media_url,
                              saved_name, match.group(3)
                              )

    def get_inline_video_poster(self, match):
        """Saves an inline video poster if not saved yet. Returns the new
        <video> tag or the original one in case of download errors."""
        poster_url, poster_filename = self._parse_url_match(match)
        if not poster_filename or not poster_url.startswith('http'):
            return match.group(0)
        saved_name = self.download_media(poster_url, filename=poster_filename)
        if saved_name is None:
            return match.group(0)
        # get rid of autoplay and muted attributes to align with normal video
        # download behaviour
        return ('%s%s/%s%s' % (match.group(1), self.media_url,
                               saved_name, match.group(3)
                               )).replace('autoplay="autoplay"', '').replace('muted="muted"', '')

    def get_inline_video(self, match):
        """Saves an inline video if not saved yet. Returns the new <video> tag
        or the original one in case of download errors."""
        video_url, video_filename = self._parse_url_match(match)
        if not video_filename or not video_url.startswith('http'):
            return match.group(0)
        saved_name = None
        if '.tumblr.com' in video_url:
            saved_name = self.get_media_url(video_url, '.mp4')
        elif options.save_video:
            saved_name = self.get_youtube_url(video_url)
        if saved_name is None:
            return match.group(0)
        return '%s%s%s' % (match.group(1), saved_name, match.group(3))

    def get_filename(self, parsed_url, image_names, offset=''):
        """Determine the image file name depending on image_names"""
        fname = urlbasename(parsed_url.path)
        ext = posix_split_ext(fname)[1]
        if parsed_url.query:
            # Insert the query string to avoid ambiguity for certain URLs (e.g. SoundCloud embeds).
            query_sep = '@' if os.name == 'nt' else '?'
            if ext:
                extwdot = '.{}'.format(ext)
                fname = fname[:-len(extwdot)] + query_sep + parsed_url.query + extwdot
            else:
                fname = fname + query_sep + parsed_url.query
        if image_names == 'i':
            return self.ident + offset + ext
        if image_names == 'bi':
            return self.backup_account + '_' + self.ident + offset + ext
        # delete characters not allowed under Windows
        return re.sub(r'[:<>"/\\|*?]', '', fname) if os.name == 'nt' else fname

    def download_media(self, url, filename=None, offset='', extension=None):
        parsed_url = urlparse(url, 'http')
        hostname = parsed_url.hostname
        if parsed_url.scheme not in ('http', 'https') or not hostname:
            return None  # This URL does not follow our basic assumptions

        # Make a sane directory to represent the host
        try:
            hostname = hostname.encode('idna').decode('ascii')
        except UnicodeError:
            hostname = hostname
        if hostname in ('.', '..'):
            hostname = hostname.replace('.', '%2E')
        if parsed_url.port not in (None, (80 if parsed_url.scheme == 'http' else 443)):
            hostname += '{}{}'.format('+' if os.name == 'nt' else ':', parsed_url.port)

        def get_path(media_dir, image_names, hostdirs):
            if filename is not None:
                fname = filename
            else:
                fname = self.get_filename(parsed_url, image_names, offset)
                if extension is not None:
                    fname = os.path.splitext(fname)[0] + extension
            parts = (media_dir,) + ((hostname,) if hostdirs else ()) + (fname,)
            return parts

        path_parts = get_path(self.media_dir, options.image_names, options.hostdirs)
        media_path = file_path_to(*path_parts)

        # prevent racing of existence check and download
        with downloading_media_cond:
            while media_path in downloading_media:
                downloading_media_cond.wait()
            downloading_media.add(media_path)

        try:
            return self._download_media_inner(url, get_path, path_parts, media_path)
        finally:
            with downloading_media_cond:
                downloading_media.remove(media_path)
                downloading_media_cond.notify_all()

    def _download_media_inner(self, url, get_path, path_parts, media_path):
        self.post_media.add(url)

        if self.prev_archive is None:
            cpy_res = False
        else:
            assert self.pa_options is not None
            pa_path_parts = get_path(
                os.path.join(post_dir, self.ident) if self.pa_options['dirs'] else media_dir,
                self.pa_options['image_names'], self.pa_options['hostdirs'],
            )
            cpy_res = maybe_copy_media(self.prev_archive, path_parts, pa_path_parts)
        file_exists = os.path.exists(media_path)
        if not (cpy_res or file_exists):
            if options.no_get:
                return None
            # We don't have the media and we want it
            assert wget_retrieve is not None
            dstpath = open_file(lambda f: f, path_parts)
            try:
                wget_retrieve(url, dstpath, post_id=self.ident, post_timestamp=self.post['timestamp'])
            except WGError as e:
                e.log()
                return None
        if file_exists:
            try:
                st = os.stat(media_path)
            except FileNotFoundError:
                pass  # skip
            else:
                if st.st_mtime > self.post['timestamp']:
                    touch(media_path, self.post['timestamp'])

        return path_parts[-1]

    def get_post(self):
        """returns this post in HTML"""
        typ = ('liked-' if options.likes else '') + self.typ
        post = self.post_header + '<article class=%s id=p-%s>\n' % (typ, self.ident)
        post += '<header>\n'
        if options.likes:
            post += '<p><a href=\"https://{0}.tumblr.com/\" class=\"tumblr_blog\">{0}</a>:</p>\n'.format(self.creator)
        post += '<p><time datetime=%s>%s</time>\n' % (self.isodate, strftime('%x %X', self.tm))
        post += '<a class=llink href={}>¶</a>\n'.format(posix_path_join(save_dir, post_dir, self.llink))
        post += '<a href=%s>●</a>\n' % self.shorturl
        if self.reblogged_from and self.reblogged_from != self.reblogged_root:
            post += '<a href=%s>⬀</a>\n' % self.reblogged_from
        if self.reblogged_root:
            post += '<a href=%s>⬈</a>\n' % self.reblogged_root
        post += '</header>\n'
        content = self.get_content()
        if self.title:
            post += '<h2>%s</h2>\n' % self.title
        post += content
        foot = []
        if self.tags:
            foot.append(''.join(self.tag_link(t) for t in self.tags))
        if self.source_title and self.source_url:
            foot.append('<a title=Source href=%s>%s</a>' %
                (self.source_url, self.source_title)
            )

        notes_html = ''

        if options.save_notes or options.copy_notes:
            if TYPE_CHECKING:
                from bs4 import BeautifulSoup
            else:
                BeautifulSoup = load_bs4('save notes' if options.save_notes else 'copy notes')

        if options.copy_notes:
            # Copy notes from prev_archive (or here)
            prev_archive = save_folder if options.reuse_json else self.prev_archive
            assert prev_archive is not None
            try:
                with open(join(prev_archive, post_dir, self.ident + post_ext)) as post_file:
                    soup = BeautifulSoup(post_file, 'lxml')
            except FileNotFoundError:
                pass  # skip
            else:
                notes = cast(Tag, soup.find('ol', class_='notes'))
                if notes is not None:
                    notes_html = ''.join([n.prettify() for n in notes.find_all('li')])

        if options.save_notes and self.backup_account not in disable_note_scraper and not notes_html.strip():
            import note_scraper

            # Scrape and save notes
            while True:
                ns_stdout_rd, ns_stdout_wr = multiprocessing.Pipe(duplex=False)
                ns_msg_queue: SimpleQueue[Tuple[LogLevel, str]] = multiprocessing.SimpleQueue()
                try:
                    args = (ns_stdout_wr, ns_msg_queue, self.url, self.ident,
                            options.no_ssl_verify, options.user_agent, options.cookiefile, options.notes_limit,
                            options.use_dns_check)
                    process = multiprocessing.Process(target=note_scraper.main, args=args)
                    process.start()
                except:
                    ns_stdout_rd.close()
                    ns_msg_queue._reader.close()  # type: ignore[attr-defined]
                    raise
                finally:
                    ns_stdout_wr.close()
                    ns_msg_queue._writer.close()  # type: ignore[attr-defined]

                try:
                    try:
                        while True:
                            level, msg = ns_msg_queue.get()
                            logger.log(level, msg)
                    except EOFError:
                        pass  # Exit loop
                    finally:
                        ns_msg_queue.close()  # type: ignore[attr-defined]

                    with ConnectionFile(ns_stdout_rd) as stdout:
                        notes_html = stdout.read()

                    process.join()
                except:
                    process.terminate()
                    process.join()
                    raise

                if process.exitcode == 2:  # EXIT_SAFE_MODE
                    # Safe mode is blocking us, disable note scraping for this blog
                    notes_html = ''
                    with disablens_lock:
                        # Check if another thread already set this
                        if self.backup_account not in disable_note_scraper:
                            disable_note_scraper.add(self.backup_account)
                            logger.info('[Note Scraper] Blocked by safe mode - scraping disabled for {}\n'.format(
                                self.backup_account
                            ))
                elif process.exitcode == 3:  # EXIT_NO_INTERNET
                    no_internet.signal()
                    continue
                break

        notes_str = '{} note{}'.format(self.note_count, 's'[self.note_count == 1:])
        if notes_html.strip():
            foot.append('<details><summary>{}</summary>\n'.format(notes_str))
            foot.append('<ol class="notes">')
            foot.append(notes_html)
            foot.append('</ol></details>')
        else:
            foot.append(notes_str)

        if foot:
            post += '\n<footer>{}</footer>'.format('\n'.join(foot))
        post += '\n</article>\n'
        return post

    @staticmethod
    def tag_link(tag):
        tag_disp = xml_escape(TAG_FMT.format(tag))
        if not TAGLINK_FMT:
            return tag_disp + ' '
        url = TAGLINK_FMT.format(domain=blog_name, tag=urlquote(to_bytes(tag)))
        return '<a href=%s>%s</a>\n' % (url, tag_disp)

    def get_path(self):
        return (post_dir, self.ident, DIR_INDEX_FILENAME) if options.dirs else (post_dir, self.file_name)

    def save_post(self):
        """saves this post locally"""
        if options.json and not options.reuse_json:
            with open_text(JSON_DIR, self.ident + '.json') as f:
                f.write(self.get_json_content())
        path_parts = self.get_path()
        try:
            with open_text(*path_parts) as f:
                f.write(self.get_post())
            os.utime(file_path_to(*path_parts), (self.date, self.date))
        except Exception:
            logger.error('Caught exception while saving post {}:\n{}'.format(self.ident, traceback.format_exc()))
            return False
        return True

    def get_json_content(self):
        return json.dumps(self.post, sort_keys=True, indent=4, separators=(',', ': '))

    @staticmethod
    def _parse_url_match(match, transform=None):
        url = match.group(2)
        if url.startswith('//'):
            url = 'https:' + url
        if transform is not None:
            url = transform(url)
        filename = urlbasename(urlparse(url).path)
        return url, filename
