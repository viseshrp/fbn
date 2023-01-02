"""Console script for nest_reset."""
import click

from .fb import check_and_notify
from . import __version__


@click.command(context_settings=dict(help_option_names=["-h", "--help"]))
@click.version_option(
    __version__,
    "-v",
    "--version",
)
@click.option(
    "--group",
    "target",
    flag_value="group",
    default=True,
    help="Monitor a Facebook group",
)
@click.option("--page", "target", flag_value="page", help="Monitor a Facebook page")
@click.option(
    "-i",
    "--id",
    "target_id",
    type=str,
    required=True,
    help="The Facebook group/page name or id",
)
@click.option(
    "-u",
    "--username",
    type=str,
    default="",
    envvar="FBN_FB_USERNAME",
    help="Your Facebook username",
)
@click.option(
    "-p",
    "--password",
    type=str,
    default="",
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
    required=False,
    help="Path to the Facebook cookies file",
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
        target, target_id, username, password, cookies_file, frequency, apprise_url, verbose
):
    """
    Simple CLI tool to look for new posts in a Facebook group or page and
    then send you a notification. Public pages/groups do not require authentication information.

    Example usage:

    $ export FBN_FB_PASSWORD=password

    $ fbn --group --id 1092319230 --every 3h --username username \
        --apprise-url mailto://gmailusername:gmailpassword@gmail.com
    """
    try:
        check_and_notify(
            target,
            target_id,
            username,
            password,
            cookies_file,
            frequency,
            apprise_url,
            verbose
        )
    except Exception as e:
        # all other exceptions
        click.echo(e)


if __name__ == "__main__":
    main()
