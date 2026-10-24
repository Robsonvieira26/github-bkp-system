import pytest


@pytest.fixture
def make_settings(tmp_path):
    from app.config import Settings

    def factory(**overrides):
        values = dict(
            github_token="test-token",
            smtp_host="smtp.test",
            smtp_user="user",
            smtp_password="pass",
            mail_from="bkp@test.local",
            mail_to="a@test.local, b@test.local",
            data_dir=tmp_path / "data",
        )
        values.update(overrides)
        return Settings(**values)

    return factory
