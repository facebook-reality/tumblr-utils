import configparser

from argparse import Namespace
from collections import dataclass
from enum import auto, Enum, IntEnum
from typing import Bool, List


class VideoType(IntEnum):
    NONE: auto()
    TUMBLR: auto()
    ALL: auto()


class ImageFilenameFormat(Enum):
    ORIGINAL: "o"
    POST_ID: "i"
    BLOG_NAME_AND_POST_ID: "bi"


@dataclass
class Configuration:
    """
    The fact that we can split these attributes into sections points to a code smell:
    these should be smaller config classes that we could pass to different modules and reduce the maintenance surface.
    But that's low priority at the moment
    """

    # Basic setttings
    api_key: str
    blog_names: List[str]

    # General settings
    create_tags_index: Bool
    max_notes_saved: int
    post_clobber: Bool

    # Filtering settings
    save_original_posts: Bool
    save_reblogs: Bool
    post_tags_whitelist: List[str]
    post_types_whitelist: List[str]
    max_posts: int
    save_period: List[int]

    # Download settings
    save_images: Bool
    save_video: VideoType
    save_audio: Bool
    image_filename_format: ImageFilenameFormat
    num_threads: int

    # Logging settings
    show_progress: Bool

    # Output settings
    output_path: str
    num_posts_per_page: int

    # Networking settings
    cookie_file_path: str
    check_dns: Bool
    ssl_verify: Bool
    user_agent: str

    # Inferable settings
    save_notes: Bool


class ConfigurationFactory:
    """
    Translates both sources of settings (CLI args, settings.ini files) into a unified Configuration instance.
    Takes a Namespace object (created by argparse.ArgumentParser.parse_args) and checks if we're using the CLI to pass args.
    The CLI takes precedence over the settings file
    """
    def __init__(self, cli_args: Namespace):
        if not any(vars(cli_args)):
            self._parse_settings_file()
        else:
            self._parse_cli_args(cli_args)

        self.validate()

    def _parse_settings_file(self):
        file_config = configparser.ConfigParser()
        file_config.read('settings.ini')

        config_dict = {
            "api_key": file_config.get("basic", "api_key"),
            "blog_names": file_config.get("basic", "blog_names").split(","),

            "create_tags_index": file_config.getboolean("general", "create_tags_index"),
            "max_notes_saved": file_config.getint("general", "max_notes_saved"),
            "post_clobber": file_config.getboolean("general", "post_clobber"),

            "save_original_posts": file_config.getboolean("filtering", "save_original_posts"),
            "save_reblogs": file_config.getboolean("filtering", "save_reblogs"),
            "post_tags_whitelist": file_config.get("filtering", "save_posts_with_tags").split(","),
            "post_types_whitelist": file_config.get("filtering", "save_posts_of_types").split(","),
            "max_posts": file_config.getint("filtering", "max_posts"),
            "save_period": file_config.get("filtering", "save_period"),

            "save_images": file_config.getboolean("download", "save_images"),
            "save_video": file_config.getboolean("download", "save_video"),
            "save_audio": file_config.getboolean("download", "save_audio"),
            "image_filename_format": file_config.get("download", "image_filename_format"),
            "num_threads": file_config.getint("download", "threads"),

            "show_progress": file_config.getboolean("logging", "show_progress"),

            "output_path": file_config.get("output", "path"),
            "num_posts_per_page": file_config.getint("output", "posts_per_page"),

            "cookie_file_path": file_config.get("networking", "cookie_file_path"),
            "check_dns": file_config.getboolean("networking", "check_dns"),
            "ssl_verify": file_config.getboolean("networking", "ssl_verify"),
            "user_agent": file_config.get("networking", "user_agent"),

            "save_notes": file_config.getint("general", "max_notes_saved") > 0
        }

        return Configuration(**config_dict)

    def _parse_cli_args(self, args: Namespace):
        config_dict = {
            "output_path": args.get("outdir", ""),
            "save_each_post_in_dirs": args.dirs,
            "show_progress": not args.quiet,
            "save_likes": args.likes,
            "save_images": args.save_images,
            "save_video": VideoType.ALL if args.save_video else VideoType.TUMBLR if args.save_video_tumblr else VideoType.NONE,
            "save_audio": args.save_audio,
            "save_notes": args.save_notes,
            "copy_notes": args.copy_notes,
            "max_notes_saved": args.notes_limit,
            "cookie_file_path": args.cookiefile,
            "save_json": args.json,
            "save_blosxom": args.blosxom,
            "reverse_monthly_archives": args.reverse_month,
            "reverse_index": args.reverse_index,
            "create_tags_index": args.tag_index,
            "max_posts": args.count,
            "skip_first_num_posts": args.skip,
            "save_period": args.period,
            "num_posts_per_page": args.posts_per_page,
            "request": args.request,
            "post_tags_whitelist": args.tags,
            "post_types_whitelist": args.type,
            "jq_filter": args.filter,
            "image_filename_format": args.image_names,
            "exif_tags": args.exif,
            "ssl_verify": not args.no_ssl_verify,
            "previous_archives": args.prev_archives,
            "post_clobber": not args.no_post_clobber,
            "use_server_timestamps": args.use_server_timestamps,
            "create_hostdirs": args.hostdirs,
            "user_agent": args.user_agent,
            "check_dns": not args.skip_dns_check,
            "num_threads": args.threads,
            "ignore_diffopt": args.ignore_diffopt,
            "fetch_missing_files": not args.no_get,
            "use_internet_archive": args.internet_archive,
            "create_media_list": args.media_list,
            "create_post_ids_file": args.id_file,
            "fetch_blog_info": args.json_info,
            "blog_names": args.blogs
        }

        return Configuration(**config_dict)
