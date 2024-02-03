POST_TYPES = ('text', 'quote', 'link', 'answer', 'video', 'audio', 'photo', 'chat')
TYPE_ANY = 'any'
TAG_ANY = '__all__'

MAX_POSTS = 50
REM_POST_INC = 10

ARCHIVE_DIR = 'archive'
JSON_DIR = 'json'
TAG_INDEX_DIR = 'tags'
THEME_DIR = 'theme'

FILE_ENCODING = 'utf-8'

BACKUP_CSS_FILENAME = 'backup.css'
CUSTOM_CSS_FILENAME = 'custom.css'
DIR_INDEX_FILENAME = 'index.html'

AVATAR_BASE = 'avatar'

TAG_FMT = '#{}'  # Format of displayed tags
TAGLINK_FMT = 'https://{domain}/tagged/{tag}'  # Format of tag link URLs; set to None to suppress the links.

# exit codes
EXIT_SUCCESS = 0
EXIT_NOPOSTS = 1
# EXIT_ARGPARSE = 2 -- returned by argparse
EXIT_INTERRUPT = 3
EXIT_ERRORS = 4
