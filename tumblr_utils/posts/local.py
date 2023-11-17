from typing import List, Tuple

import os
import re
import time

from posixpath import join as posix_path_join

from tumblr_utils.constants import DIR_INDEX_FILENAME, FILE_ENCODING


class LocalPost:
    def __init__(self, post_file):
        self.post_file = post_file
        if options.tag_index:
            with open(post_file, encoding=FILE_ENCODING) as f:
                post = f.read()
            # extract all URL-encoded tags
            self.tags: List[Tuple[str, str]] = []
            footer_pos = post.find('<footer>')
            if footer_pos > 0:
                self.tags = re.findall(r'<a.+?/tagged/(.+?)>#(.+?)</a>', post[footer_pos:])
        parts = post_file.split(os.sep)
        if parts[-1] == DIR_INDEX_FILENAME:  # .../<post_id>/index.html
            self.file_name = os.path.join(*parts[-2:])
            self.ident = parts[-2]
        else:
            self.file_name = parts[-1]
            self.ident = os.path.splitext(self.file_name)[0]
        self.date: float = os.stat(post_file).st_mtime
        self.tm = time.localtime(self.date)

    def get_post(self, in_tag_index):
        with open(self.post_file, encoding=FILE_ENCODING) as f:
            post = f.read()
        # remove header and footer
        lines = post.split('\n')
        while lines and '<article ' not in lines[0]:
            del lines[0]
        while lines and '</article>' not in lines[-1]:
            del lines[-1]
        post = '\n'.join(lines)
        if in_tag_index:
            # fixup all media links which now have to be two folders lower
            shallow_media = posix_path_join('..', media_dir)
            deep_media = posix_path_join(save_dir, media_dir)
            post = post.replace(shallow_media, deep_media)
        return post
