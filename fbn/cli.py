"""Console script"""
import click

from . import __version__
from .fb import check_and_notify


@click.command(context_settings=dict(help_option_names=["-h", "--help"]))
@click.version_option(
    __version__,
    "-v",
    "--version",
)
@click.option(
    "-i",
    "--id",
    "target_id",
    type=str,
    required=True,
    help="The Facebook group name or id",
)
@click.option(
    "-u",
    "--username",
    type=str,
    default="",
    show_default=True,
    envvar="FBN_FB_USERNAME",
    help="Your Facebook username",
)
@click.option(
    "-p",
    "--password",
    type=str,
    default="",
    show_default=True,
    envvar="FBN_FB_PASSWORD",
    help="Your Facebook password",
)
@click.option(
    "-c",
    "--cookies-file",
    type=click.Path(
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
        allow_dash=False,
    ),
    default="facebook.com_cookies.txt",
    show_default=True,
    help="Path to the Facebook cookies file",
)
@click.option(
    "-g",
    "--user-agent",
    type=str,
    required=False,
    help="User agent to use for scraping",
)
@click.option(
    "-s",
    "--sample-count",
    type=int,
    default=10,
    show_default=True,
    help="Number of posts to sample",
)
@click.option(
    "-e",
    "--every",
    "frequency",
    type=str,
    default="1h",
    show_default=True,
    help="Monitor frequency",
)
@click.option(
    "-a",
    "--apprise-url",
    type=str,
    required=True,
    envvar="FBN_APPRISE_URL",
    help="The apprise URL to notify",
)
@click.option(
    "-v", "--verbose", is_flag=True, default=False, help="Enable debug logging."
)
def main(
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
    """
    Simple CLI tool to look for new posts in a Facebook group and
    then send you a notification. Public groups do not require authentication information.

    Example usage:

    $ export FBN_APPRISE_URL=mailto://gmailusername:gmailpassword@gmail.com

    $ fbn --id craigslist --every 45m --cookies-file facebook.com_cookies.txt --verbose
    """
    try:
        check_and_notify(
            target_id,
            username,
            password,
            cookies_file,
            user_agent,
            sample_count,
            frequency,
            apprise_url,
            verbose,
        )
    except Exception as e:
        # all other exceptions
        click.echo(e)


if __name__ == "__main__":
    main()
