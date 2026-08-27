-- Bambu Monitor: database + app user, for MariaDB or MySQL.
-- Run this ONCE, as the server's root user (e.g. via phpMyAdmin, or the CLI).
-- The application creates its own tables on first start; this only provisions the
-- database and a least-privilege user.
--
-- >>> Replace 'REPLACE_WITH_STRONG_PASSWORD' below, then type the same value
--     into the setup wizard's first page. Nothing else needs it. <<<
--
-- Works as-is on MariaDB and on MySQL 8. On MySQL 8 the user gets the default
-- caching_sha2_password plugin, which the app can use as long as `cryptography`
-- is installed (it is in requirements.txt). If you would rather not have that
-- dependency, create the user with the older plugin instead:
--     CREATE USER 'bambu'@'127.0.0.1'
--       IDENTIFIED WITH mysql_native_password BY 'REPLACE_WITH_STRONG_PASSWORD';

CREATE DATABASE IF NOT EXISTS bambu_monitor
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- The app connects over TCP to 127.0.0.1; grant both host forms so it works
-- whether MariaDB resolves the connection as 'localhost' (socket) or '127.0.0.1'.
CREATE USER IF NOT EXISTS 'bambu'@'localhost'  IDENTIFIED BY 'REPLACE_WITH_STRONG_PASSWORD';
CREATE USER IF NOT EXISTS 'bambu'@'127.0.0.1'  IDENTIFIED BY 'REPLACE_WITH_STRONG_PASSWORD';

GRANT ALL PRIVILEGES ON bambu_monitor.* TO 'bambu'@'localhost';
GRANT ALL PRIVILEGES ON bambu_monitor.* TO 'bambu'@'127.0.0.1';

FLUSH PRIVILEGES;
