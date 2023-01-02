import sys

from facebook_scraper import get_posts, enable_logging, set_user_agent
import logging
from .exceptions import NoAuthInfoException

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
file_handler = logging.FileHandler('fb_debug.txt')
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

stream_handler = logging.StreamHandler(stream=sys.stdout)
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)


def check_and_notify(target, target_id, username, password, cookies_file, frequency, apprise_url, verbose):

    if verbose:
        enable_logging()
    # todo
    # set_user_agent()
    # todo: start small and request more
    kwargs = {
        "page_limit": 1,
        "options": {"posts_per_page": 5}
    }
    if target == "page":
        kwargs["account"] = target_id
    else:
        kwargs["group"] = target_id

    if cookies_file:
        kwargs["cookies"] = cookies_file
    else:
        if username is not None and password is not None:
            kwargs["credentials"] = (username, password)
        else:
            raise NoAuthInfoException("Please provide your Facebook username/password or a cookies file.")

    posts = get_posts(**kwargs)


def fetch_posts():
    pass
