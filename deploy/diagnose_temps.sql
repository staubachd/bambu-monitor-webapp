-- Diagnostic: run this in phpMyAdmin BEFORE/AFTER the cleanup to see the truth.
--
-- If `bad_noz0`/`bad_noz1` are 0 but the chart is still slow, the packed rows
-- are not the cause. If `newest_row` is not "a few seconds ago", the app on the
-- NAS is not recording (stopped, or recording mode is auto/off while idle).

SELECT COUNT(*)                            AS total_rows,
       SUM(noz0    > 1000)                 AS bad_noz0,
       SUM(noz1    > 1000)                 AS bad_noz1,
       SUM(chamber > 1000)                 AS bad_chamber,
       MAX(noz0)                           AS max_noz0,
       MAX(noz1)                           AS max_noz1,
       FROM_UNIXTIME(MIN(ts))              AS oldest_row,
       FROM_UNIXTIME(MAX(ts))              AS newest_row
FROM bambu_monitor.telemetry;
