import logging
import re
import time
from datetime import datetime

import apprise
import schedule
from facebook_scraper import get_posts, enable_logging, set_user_agent
from facebook_scraper.exceptions import TemporarilyBanned, AccountDisabled
from tenacity import (
    retry,
    stop_after_attempt,
    retry_if_exception_type,
    before_log,
    after_log,
    wait_chain,
    wait_fixed,
)

from .constants import SCHEDULE_UNIT_MAP
from .exceptions import NoAuthInfoException, InvalidFrequencyException

# Disable logging by default
logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())
# current post set
current_post_set = None


def parse_frequency(frequency):
    count, unit = None, None
    regex = re.compile(r"(\d+)([mhdw])")
    matches = regex.findall(frequency)
    if len(matches) == 1:
        count, unit = matches[0]
    if count is None or unit not in SCHEDULE_UNIT_MAP.keys():
        raise InvalidFrequencyException(
            f"The provided monitor frequency '{frequency}' is invalid."
        )
    return count, unit


@retry(
    retry=retry_if_exception_type(TemporarilyBanned),
    stop=stop_after_attempt(3),
    wait=wait_chain(wait_fixed(600), wait_fixed(700), wait_fixed(800)),
    reraise=True,
    before=before_log(logger, logging.DEBUG),
    after=after_log(logger, logging.DEBUG),
)
def get_latest_posts(**kwargs):
    sample_count = kwargs.pop("sample_count")
    posts = {}
    try:
        for post in get_posts(**kwargs):
            post_id = post["post_id"]
            posts[post_id] = {
                "text": post["text"],
                "post_text": post["post_text"],
                "post_url": post["post_url"],
                "likes": post["likes"],
                "comments": post["comments"],
                "username": post["username"],
            }
            logger.debug(f"Obtained post {post_id}")
            if len(posts) == sample_count:
                logger.debug(f"Stopping with {sample_count} posts...")
                break
    except (TemporarilyBanned, AccountDisabled) as e:
        raise e
    return posts


def notify(apprise_url, title, body):
    app_obj = apprise.Apprise()
    app_obj.add(apprise_url)
    app_obj.notify(
        title=title,
        body=body,
    )


def monitor_fb(**kwargs):
    global current_post_set
    apprise_url = kwargs.pop("apprise_url")
    logger.debug(f"Fetching latest posts...")
    latest_posts_info = get_latest_posts(**kwargs)
    if latest_posts_info:
        logger.debug(f"Finished fetching latest posts")
        latest_post_set = set(latest_posts_info.keys())
        if current_post_set is None:
            logger.debug(f"Updating current_post_set for the first time and exiting...")
            current_post_set = latest_post_set
        else:
            logger.debug(f"Getting new posts...")
            new_post_set = latest_post_set - current_post_set
            if not new_post_set:
                logger.debug(f"No new posts found. Ending...")
                return
            current_post_set = latest_post_set
            logger.debug(f"Obtained {len(new_post_set)} new posts.")
            group_name = kwargs['group']
            # email digests
            is_email = (
                apprise_url.startswith("mailto://")
                or apprise_url.startswith("mailgun://")
                or apprise_url.startswith("sendgrid://")
            )
            body = f"Found at {datetime.now()}"
            if is_email:
                body = """
                <!DOCTYPE html>
                <html>
                    <body>
                        <div>
                            <div>
                """
                for new_post_id in new_post_set:
                    post = latest_posts_info[new_post_id]
                    logger.debug(f"Getting post '{new_post_id}' from {group_name}")
                    body += f"""
                                    <div style="text-align:center;">
                                        <h3>{post["username"]}</h3>
                                        <p style="font-size: 1.2rem;">
                                        {post.get("text") or post.get("post_text")}
                                        </p>
                                        <p>{post["comments"]} comments</p>
                                        <p>{post["likes"]} likes</p>
                                        <a href="{post["post_url"]}">Read more</a>
                                    </div><br><br>
                    """
                body += """
                            </div>
                        </div>
                    </body>
                </html>    
                """
            # notify
            title = f"{len(new_post_set)} new post(s) from {group_name}"
            logger.debug(f"Notifying user of new posts : {title}")
            notify(apprise_url, title, body)


def check_and_notify(
    target_id,
    username,
    password,
    cookies_file,
    user_agent,
    sample_count,
    frequency,
    apprise_url,
    verbose,
):
    if verbose:
        # fbn logging
        level = logging.DEBUG
        formatter = logging.Formatter(
            "<%(asctime)s><%(name)s><%(levelname)s> %(message)s"
        )
        handler = logging.StreamHandler()
        handler.setLevel(level)
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(level)
        # fb scraper logging
        enable_logging(level=level)
    else:
        # suppress warnings
        import warnings

        warnings.filterwarnings("ignore")

    if user_agent:
        set_user_agent(user_agent)
    kwargs = {
        "group": target_id,
        "page_limit": None,
        "extra_info": False,
        "options": {"allow_extra_requests": False, "posts_per_page": sample_count},
        "apprise_url": apprise_url,
        "sample_count": sample_count,
    }

    if cookies_file:
        kwargs["cookies"] = cookies_file
    else:
        if username is not None and password is not None:
            kwargs["credentials"] = (username, password)
        else:
            raise NoAuthInfoException(
                "Please provide your Facebook username/password or a cookies file."
            )

    logger.debug("Creating schedule...")
    if frequency:
        interval, unit = parse_frequency(frequency)
        schedule_unit = SCHEDULE_UNIT_MAP[unit]
        job = getattr(schedule.every(int(interval)), schedule_unit).do(monitor_fb, **kwargs)
    else:  # randomize as the default
        job = schedule.every(2).to(4).hours.do(monitor_fb, **kwargs)
    logger.debug(f"Running once...")
    schedule.run_all()
    logger.debug(f"Starting schedule {job}...")
    while True:
        time.sleep(600)
        schedule.run_pending()
