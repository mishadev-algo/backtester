BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout = '12s';
SELECT json_build_object(
 'source', 'VPS PostgreSQL · read only',
 'fetched_at', CURRENT_TIMESTAMP,
 'accounts', (SELECT COALESCE(json_agg(t), '[]'::json) FROM (
   SELECT server, login::text, currency, day_timezone, host_id, terminal_id,
          latest_snapshot_utc, history_start_day, balance FROM accounts ORDER BY server, login
 ) t),
 'deals', (SELECT COALESCE(json_agg(t), '[]'::json) FROM (
   SELECT server, login::text, ticket::text, time_utc, day_local, type, kind, entry,
          position_id::text, symbol, magic::text, strategy, comment,
          profit, commission, swap, fee, volume FROM deals ORDER BY time_utc, server, login, ticket
 ) t),
 'positions', (SELECT COALESCE(json_agg(t), '[]'::json) FROM (
   SELECT server, login::text, ticket::text, symbol, type, magic::text, strategy, volume,
          price_open, price_current, profit, swap FROM positions ORDER BY server, login, ticket
 ) t),
 'backtests', (SELECT COALESCE(json_agg(t), '[]'::json) FROM (
   SELECT run_id, strategy, file_name, currency, starting_capital, first_day, last_day,
          row_count, imported_utc FROM backtest_runs ORDER BY imported_utc DESC
 ) t),
 'backtest_days', (SELECT COALESCE(json_agg(t), '[]'::json) FROM (
   SELECT run_id, day, net_pnl FROM backtest_days ORDER BY day, run_id
 ) t),
 'hosts', (SELECT COALESCE(json_agg(t), '[]'::json) FROM (
   SELECT host_id, last_heartbeat_utc FROM hosts ORDER BY host_id
 ) t),
 'terminal_status', (SELECT COALESCE(json_agg(t), '[]'::json) FROM (
   SELECT host_id, terminal_id, received_utc,
          status_json::jsonb->>'state' AS state,
          status_json::jsonb->>'connected' AS connected,
          status_json::jsonb->>'autotrading' AS autotrading,
          status_json::jsonb->>'data_complete' AS data_complete
   FROM terminal_status ORDER BY host_id, terminal_id
 ) t)
);
ROLLBACK;
