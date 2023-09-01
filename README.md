# fbn
### Tool to monitor fb groups and notify.
This was a small holiday project. I just absolutely hate being on Facebook.
The UI, the clunky android app.. Ughh. I uninstalled it a long time ago and just use the
mobile site.
I hate that I am forced to be on it, sometimes, because there is
valuable information from folks on there in some communities I am a part of. This tool
is to remove the need for me to keep watching these groups constantly. I run this using 
systemd on a Raspberry Pi 4B.

```sh
$ pip install fbn

$ fbn --help
Usage: fbn [OPTIONS]

  Simple CLI tool to look for new posts in a Facebook group and then send you
  a notification. Public groups do not require authentication information.

  Example usage:

  $ export FBN_APPRISE_URL=mailto://gmailusername:gmailpassword@gmail.com

  $ fbn --id craigslist --every 45m --cookies-file facebook.com_cookies.txt
  --verbose

Options:
  -v, --version               Show the version and exit.
  -i, --id TEXT               The Facebook group name or id  [required]
  -u, --username TEXT         Your Facebook username  [env var:
                              FBN_FB_USERNAME]
  -p, --password TEXT         Your Facebook password  [env var:
                              FBN_FB_PASSWORD]
  -c, --cookies-file FILE     Path to the Facebook cookies file
  -g, --user-agent TEXT       User agent to use for scraping
  -s, --sample-count INTEGER  Number of posts to sample  [default: 10]
  -e, --every TEXT            Monitor frequency. Of the form <int><m/h/d/w>.
                              Eg: --every 2m. Will check every 2 minutes.
  -t, --to TEXT               Monitor randomization frequency. Requires
                              --every. Same form as --every. Both units must
                              match. Eg: --every 1h --to 2h. Will randomize
                              checks every 1 to 2 hours.
  -a, --apprise-url TEXT      The apprise URL to notify  [env var:
                              FBN_APPRISE_URL; required]
  --include-errors            Notify of errors as well.
  -v, --verbose               Enable debug logging.
  -h, --help                  Show this message and exit.
```

This uses [facebook-scraper](https://github.com/kevinzg/facebook-scraper) that scrapes the target group for posts.
If the group is private, authentication is required as you must be a member,
obviously. Auth can be passed using the CLI options or the env vars `FBN_FB_USERNAME` or `FBN_FB_PASSWORD`.
Auth can also be passed in the form of cookies in Netscape or JSON format. Use the CLI option.
You can extract cookies from your browser after logging into Facebook with
an extension like [Get cookies.txt LOCALLY (Chrome)](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc/)
or [Cookie Quick Manager (Firefox)](https://addons.mozilla.org/en-US/firefox/addon/cookie-quick-manager/).
Make sure that you include both the c_user cookie and the xs cookie, 
you will get an InvalidCookies exception if you don't.

*Since this is a scraper, the more frequently you scrape, the more the chances are of getting locked out of your account
or even banned permanently. The tool detects temporary bans and backs off appropriately, but [be warned](https://github.com/kevinzg/facebook-scraper/issues/409#issuecomment-907639417).*
You may see `ConnectionResetError: [Errno 54] Connection reset by peer` for the URL `https://m.facebook.com/settings` 
from time to time. This is possibly because you are scraping too often. Reduce your frequency and/or rotate cookies 
to fix this. More info [here](https://github.com/kevinzg/facebook-scraper/issues/763).

If you do not provide a value for `--every` or `--to`, fbn will automatically randomize the checks to once 
between 1-3 hours which works out a lot better in terms of scraping frequency.

Notifications are sent through the amazing [Apprise](https://github.com/caronc/apprise) which supports a ton of 
[notification services](https://github.com/caronc/apprise/wiki#notification-services). Use the CLI option
 or env var `FBN_APPRISE_URL` to set that.

#### systemd configuration example

```sh
[Unit]
Description=fbn
After=network.target

[Service]
ExecStart=/opt/fbn/venv/bin/fbn --id andrewschapel --username myid@gmail.com --password password --verbose --sample-count 20 -a mailto://myid:token@gmail.com --include-errors
Restart=on-abort
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
```
