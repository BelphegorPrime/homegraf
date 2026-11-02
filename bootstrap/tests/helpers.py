import tempfile

from hgboot.config import Config

PASSWORD = "pw-correct-1234"


def config(tmp: str | None = None, **over) -> Config:
    tmp = tmp or tempfile.mkdtemp()
    base = dict(admin_user="admin", admin_email="admin@example.org", admin_password=PASSWORD, domain="home.lab", host_ip="192.168.1.10",
                dns_mode="external", data_dir="/srv/homegraf/data", webhook_secret="hook-secret", state_dir=tmp, seed_dir=f"{tmp}/seed",
                ports={"HTTP": "80", "NPM_ADMIN": "81", "FORGEJO": "3000", "KOMODO": "9120", "INFISICAL": "8079", "HOMEGRAF": "8097", "DNS_ADMIN": "8053"})
    base.update(over)
    return Config(**base)


def no_wait(check, what, **kw):
    """wait_for without waiting: the check must be true now (or raise), as a test of the step, not of the clock."""
    from hgboot.http import Unreachable
    try:
        ok = check()
    except Unreachable:
        ok = False
    if not ok:
        raise TimeoutError(f"{what} did not become ready within test time")
