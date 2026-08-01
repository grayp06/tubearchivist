"""
Functionality:
- read and write application config backed by ES
- encapsulate persistence of application properties
"""

from os import environ, path

try:
    from dotenv import load_dotenv

    print("loading local dotenv")
    load_dotenv(".env")
except ModuleNotFoundError:
    pass


def get_password_from_file(env_var_name) -> str:
    """get password from file"""

    env_var_file: str = env_var_name + "_FILE"

    env_var_name_val = environ.get(env_var_name)
    env_var_path_val = environ.get(env_var_file)

    if env_var_name_val is not None:
        return str(env_var_name_val)

    if env_var_path_val is None:
        print(f"either {env_var_name} or {env_var_file} must be set")
        return ""

    is_path = path.isfile(env_var_path_val)
    if not is_path:
        print(f"{env_var_path_val} is not a path")
        return ""

    with open(env_var_path_val, "r", encoding="utf-8") as f:
        file_content = f.read().strip()

    return file_content


class EnvironmentSettings:
    """
    Handle settings for the application that are driven from the environment.
    These will not change when the user is using the application.
    These settings are only provided only on startup.
    """

    HOST_UID: int = int(environ.get("HOST_UID", False))
    HOST_GID: int = int(environ.get("HOST_GID", False))
    DISABLE_STATIC_AUTH: bool = bool(environ.get("DISABLE_STATIC_AUTH"))
    TZ: str = str(environ.get("TZ", "UTC"))
    TA_PORT: int = int(environ.get("TA_PORT", False))
    TA_BACKEND_PORT: int = int(environ.get("TA_BACKEND_PORT", False))
    TA_USERNAME: str = str(environ.get("TA_USERNAME"))
    TA_PASSWORD: str = get_password_from_file("TA_PASSWORD")

    # Application Paths
    MEDIA_DIR: str = str(environ.get("TA_MEDIA_DIR", "/youtube"))
    APP_DIR: str = str(environ.get("TA_APP_DIR", "/app"))
    CACHE_DIR: str = str(environ.get("TA_CACHE_DIR", "/cache"))

    # Namespace retained for compatibility with existing state keys.
    REDIS_NAME_SPACE: str = str(environ.get("REDIS_NAME_SPACE", "ta:"))
    DATABASE_PATH: str = str(
        environ.get("TA_DATABASE_PATH", path.join(CACHE_DIR, "db.sqlite3"))
    )
    SNAPSHOT_DIR: str = str(environ.get("TA_SNAPSHOT_DIR", path.join(CACHE_DIR, "backup")))

    def get_cache_root(self):
        """get root for web server"""
        if self.CACHE_DIR.startswith("/"):
            return self.CACHE_DIR

        return f"/{self.CACHE_DIR}"

    def get_media_root(self):
        """get root for media folder"""
        if self.MEDIA_DIR.startswith("/"):
            return self.MEDIA_DIR

        return f"/{self.MEDIA_DIR}"

    def print_generic(self):
        """print generic env vars"""
        print(f"""
            HOST_UID: {self.HOST_UID}
            HOST_GID: {self.HOST_GID}
            TZ: {self.TZ}
            DISABLE_STATIC_AUTH: {self.DISABLE_STATIC_AUTH}
            TA_PORT: {self.TA_PORT}
            TA_BACKEND_PORT: {self.TA_BACKEND_PORT}
            TA_USERNAME: {self.TA_USERNAME}
            TA_PASSWORD: *****""")

    def print_paths(self):
        """debug paths set"""
        print(f"""
            MEDIA_DIR: {self.MEDIA_DIR}
            APP_DIR: {self.APP_DIR}
            CACHE_DIR: {self.CACHE_DIR}""")

    def print_redis_conf(self):
        """debug redis conf paths"""
        print(f"""
            DATABASE_PATH: {self.DATABASE_PATH}
            STATE_NAME_SPACE: {self.REDIS_NAME_SPACE}""")

    def print_es_paths(self):
        """debug es conf"""
        print(f"""
            DOCUMENT_STORE: SQLite ({self.DATABASE_PATH})""")

    def print_all(self):
        """print all"""
        self.print_generic()
        self.print_paths()
        self.print_redis_conf()
        self.print_es_paths()
