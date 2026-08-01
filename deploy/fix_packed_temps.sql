-- One-off cleanup for rows written before the packed-temperature fix.
--
-- The X2D packs (target << 16) | current into its temperature fields once a
-- target is set, so an un-decoded nozzle temperature was stored as e.g. 14418140
-- instead of 220. Those rows blow up the y-axis of the temperature chart.
--
-- Real temperatures never exceed a few hundred degrees, so anything above 1000
-- is certainly a packed value. We NULL them rather than try to decode in SQL:
-- the chart simply skips NULL points, leaving a small gap instead of a spike.
--
-- Run once in phpMyAdmin against the bambu_monitor database.

UPDATE telemetry SET noz0    = NULL WHERE noz0    > 1000;
UPDATE telemetry SET noz1    = NULL WHERE noz1    > 1000;
UPDATE telemetry SET chamber = NULL WHERE chamber > 1000;
UPDATE telemetry SET bed_cur = NULL WHERE bed_cur > 1000;
