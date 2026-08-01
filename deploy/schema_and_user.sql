-- Bambu Monitor: database + app user for the Synology MariaDB.
-- Run this ONCE, as the MariaDB root user (e.g. via phpMyAdmin, or the mysql CLI).
-- The application creates its own tables on first start; this only provisions the
-- database and a least-privilege user.
--
-- >>> Replace 'REPLACE_WITH_STRONG_PASSWORD' below AND put the same value in
--     printer.config.json -> storage.mariadb.password before starting the app. <<<

CREATE DATABASE IF NOT EXISTS bambu_monitor
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- The app connects over TCP to 127.0.0.1; grant both host forms so it works
-- whether MariaDB resolves the connection as 'localhost' (socket) or '127.0.0.1'.
CREATE USER IF NOT EXISTS 'bambu'@'localhost'  IDENTIFIED BY 'REPLACE_WITH_STRONG_PASSWORD';
CREATE USER IF NOT EXISTS 'bambu'@'127.0.0.1'  IDENTIFIED BY 'REPLACE_WITH_STRONG_PASSWORD';

GRANT ALL PRIVILEGES ON bambu_monitor.* TO 'bambu'@'localhost';
GRANT ALL PRIVILEGES ON bambu_monitor.* TO 'bambu'@'127.0.0.1';

FLUSH PRIVILEGES;
