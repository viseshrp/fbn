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
    envvar="FBN_FB_USERNAME",
    show_envvar=True,
    required=False,
    help="Your Facebook username",
)
@click.option(
    "-p",
    "--password",
    type=str,
    envvar="FBN_FB_PASSWORD",
    show_envvar=True,
    required=False,
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
    required=False,
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
    "every",
    type=str,
    required=False,
    help="Monitor frequency. Of the form <int><m/h/d/w>. "
         "Eg: --every 2m. Will check every 2 minutes.",
)
@click.option(
    "-t",
    "--to",
    "to",
    type=str,
    required=False,
    help="Monitor randomization frequency. Requires --every. Same form as --every. "
         "Both units must match. "
         "Eg: --every 1h --to 2h. Will randomize checks every 1 to 2 hours. "
)
@click.option(
    "-a",
    "--apprise-url",
    type=str,
    required=True,
    envvar="FBN_APPRISE_URL",
    show_envvar=True,
    help="The apprise URL to notify",
)
@click.option(
    "--include-errors", "on_error", is_flag=True, default=False, help="Notify of errors as well."
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
    every,
    to,
    apprise_url,
    on_error,
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
            every,
            to,
            apprise_url,
            on_error,
            verbose,
        )
    except Exception as e:
        # all other exceptions
        click.echo(e)


if __name__ == "__main__":
    main()
