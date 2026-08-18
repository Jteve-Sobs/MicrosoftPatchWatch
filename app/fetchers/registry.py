from app.fetchers.base import BaseFetcher
from app.fetchers.dotnet import DotNetFetcher
from app.fetchers.msrc import MsrcDotNetFrameworkFetcher
from app.fetchers.sql_server import SqlServerFetcher
from app.fetchers.windows_release_health import WindowsReleaseHealthFetcher


def get_fetchers() -> list[BaseFetcher]:
    """Order matters a little: cheap/reliable sources first, so a slow/flaky
    source at the end doesn't delay the ones users care about most."""
    return [
        WindowsReleaseHealthFetcher(),
        MsrcDotNetFrameworkFetcher(),
        DotNetFetcher(),
        SqlServerFetcher(),
    ]
