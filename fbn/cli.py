"""Console script for nest_reset."""
import click

from .fb import check_and_notify
from . import __version__


@click.command(context_settings=dict(help_option_names=['-h', '--help']))
@click.version_option(__version__, '-v', '--version', )
@click.option(
    '--group',
    'target',
    flag_value='group',
    default=True,
    help="Monitor a facebook group"
)
@click.option(
    '--page',
    'target',
    flag_value='page',
    help="Monitor a facebook page"
)
@click.option(
    '-i',
    '--id',
    'target_id',
    required=True,
    type=str,
    help="The facebook group/page name or id"
)
@click.option(
    '-u',
    '--username',
    prompt="Please enter your facebook username",
    hide_input=False,
    confirmation_prompt=True,
    required=False,
    type=str,
    envvar="FBN_FB_USERNAME",
    help="Your facebook username"
)
@click.option(
    '-p',
    '--password',
    prompt="Please enter your facebook password",
    hide_input=True,
    confirmation_prompt=True,
    required=False,
    type=str,
    envvar="FBN_PASSWORD",
    help="Your facebook password"
)
@click.option(
    '-c',
    '--cookies-file',
    type=click.Path(exists=True, file_okay=True, dir_okay=False,
                    readable=True, resolve_path=True, allow_dash=False),
    required=False,
    help="Path to the facebook cookies file"
)
@click.option(
    '-e',
    '--every',
    'frequency',
    type=str,
    default="1h",
    show_default=True,
    help="Monitor frequency"
)
@click.option(
    '-a',
    '--apprise-url',
    prompt="Please paste your apprise URL here (hidden)",
    hide_input=True,
    confirmation_prompt=True,
    required=True,
    type=str,
    envvar="FBN_APPRISE_URL",
    help="The apprise URL to notify"
)
@click.option(
    '-v',
    '--verbose',
    is_flag=True,
    required=False,
    help="Enable debug logging."
)
def main(*args):
    """
    Simple CLI tool to look for new posts in a facebook group or page and
    then send you a notification. Public pages/groups do not require authentication information.

    Example usage:

    $ export FBN_FB_PASSWORD=password

    $ fbn --group --id 1092319230 --every 3h --username username \
        --apprise-url mailto://gmailusername:gmailpassword@gmail.com
    """
    try:
        check_and_notify(*args)
    except Exception as e:
        # all other exceptions
        click.echo(e)


if __name__ == "__main__":
    main()
