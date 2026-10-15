BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout = '12s';
WITH config AS (
 SELECT convert_from(decode('__FILTERS_BASE64__', 'base64'), 'UTF8')::jsonb p
), scope AS (
 SELECT p, COALESCE(NULLIF(p->>'before','')::timestamptz,CURRENT_TIMESTAMP) as_of FROM config
), __LOG_CLASSIFICATION__
scoped AS MATERIALIZED (
 SELECT e.event_id,e.received_utc,e.host_id,e.terminal_id,e.stream,e.raw_line,e.strategy,e.strategy_source,e.severity,e.severity_source
 FROM classified e CROSS JOIN scope s
 WHERE (s.p->'host'='null'::jsonb OR (s.p->'host') ? e.host_id)
 AND (s.p->'terminal'='null'::jsonb OR (s.p->'terminal') ? (e.host_id || '|' || e.terminal_id))
 AND (s.p->>'legacy_terminal'='' OR e.terminal_id=s.p->>'legacy_terminal')
 AND (s.p->'stream'='null'::jsonb OR (s.p->'stream') ? e.stream)
 AND (s.p->'strategy'='null'::jsonb OR (s.p->'strategy') ? e.strategy)
 AND (s.p->>'q'='' OR strpos(lower(e.raw_line),lower(s.p->>'q'))>0)
 AND (s.p->>'start'='' OR left(e.received_utc,10)>=s.p->>'start')
 AND (s.p->>'end'='' OR left(e.received_utc,10)<=s.p->>'end')
 AND e.received_utc::timestamptz <= s.as_of
), filtered AS MATERIALIZED (
 SELECT e.* FROM scoped e CROSS JOIN scope s
 WHERE s.p->'severity'='null'::jsonb OR (s.p->'severity') ? e.severity
), choices AS (
 SELECT host_id,terminal_id FROM log_events GROUP BY host_id,terminal_id
 UNION SELECT host_id,terminal_id FROM accounts
), labels AS (
 SELECT c.host_id,c.terminal_id,c.host_id || '|' || c.terminal_id AS key,
 COALESCE((SELECT string_agg(DISTINCT a.login::text || ' · ' || a.server, ', ')
  FROM accounts a WHERE a.host_id=c.host_id AND a.terminal_id=c.terminal_id),
  'Терминал ' || left(c.terminal_id,10) || '…') AS label FROM choices c
), page_rows AS (
 SELECT f.*,l.label AS account FROM filtered f LEFT JOIN labels l USING(host_id,terminal_id)
 ORDER BY received_utc DESC,event_id DESC LIMIT 50 OFFSET (SELECT (p->>'page')::integer*50 FROM scope)
)
SELECT json_build_object(
 'fetched_at',CURRENT_TIMESTAMP,'as_of',(SELECT as_of FROM scope),
 'total',(SELECT count(*) FROM filtered),'page',(SELECT (p->>'page')::integer FROM scope),'page_size',50,
 'strategies',COALESCE((SELECT json_agg(strategy ORDER BY strategy) FROM (SELECT DISTINCT strategy FROM classified) st),'[]'::json),
 'severity_counts',(SELECT json_build_object('error',count(*) FILTER (WHERE severity='error'),'warning',count(*) FILTER (WHERE severity='warning'),'info',count(*) FILTER (WHERE severity='info')) FROM scoped),
 'latest',(SELECT max(received_utc) FROM filtered),
 'rows',COALESCE((SELECT json_agg(r ORDER BY received_utc DESC,event_id DESC) FROM page_rows r),'[]'::json),
 'terminals',COALESCE((SELECT json_agg(l ORDER BY label,host_id,terminal_id) FROM labels l),'[]'::json)
);
ROLLBACK;
