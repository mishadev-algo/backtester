-- Only explicit strategy identity is used: structured payload, EA source, or message prefix.
-- Never infer a strategy from the account, another row, a ticket, or an arbitrary mention.
parts AS (
 SELECT e.*,
 CASE WHEN raw_line ~ E'^[A-Z]{2}\\t[0-9]+\\t[0-9:.]+\\t[^\\t]*\\t'
      THEN split_part(raw_line,E'\t',2) ELSE '' END mt_level,
 CASE WHEN raw_line ~ E'^[A-Z]{2}\\t[0-9]+\\t[0-9:.]+\\t[^\\t]*\\t'
      THEN split_part(raw_line,E'\t',4) ELSE '' END ea_source,
 regexp_replace(raw_line,E'^[A-Z]{2}\\t[0-9]+\\t[0-9:.]+\\t[^\\t]*\\t','') message
 FROM log_events e
), payloads AS (
 SELECT p.*,CASE WHEN message LIKE 'EA_EVENT_V1 %' AND substring(message FROM 13) IS JSON OBJECT
 THEN substring(message FROM 13)::jsonb ELSE NULL END payload FROM parts p
), identities AS (
 SELECT p.*,
 CASE WHEN upper(payload->>'strategy_id') ~ '^S-[0-9]{3}$' THEN upper(payload->>'strategy_id') END payload_strategy,
 substring(upper(ea_source) FROM '^(S-[0-9]{3})(?:[^0-9]|$)') source_strategy,
 substring(upper(message) FROM '^(S-[0-9]{3})(?:[[:space:]:]|$)') message_strategy,
 upper(payload->>'level') explicit_level,
 -- Strip benign error counters/negations before using text heuristics.
 regexp_replace(lower(message),'\m(no errors?|without errors?|errors?[[:space:]]*[:=][[:space:]]*0\M|ошибок нет)\M','','g') problem_text
 FROM payloads p
), levels AS (
 SELECT i.*,
 CASE
 WHEN explicit_level IN ('ERROR','FATAL','CRITICAL') THEN 'error'
 WHEN explicit_level IN ('WARN','WARNING') THEN 'warning'
 WHEN explicit_level IN ('INFO','DEBUG','TRACE') THEN 'info'
 WHEN mt_level IN ('2','3') THEN 'error'
 WHEN mt_level='1' THEN 'warning'
 WHEN message ~* '(ORB range invalid|setup reset: EMA filter failed)' THEN 'info'
 WHEN problem_text ~* '(\m(error|fatal|exception)\M|ошибка|не удалось|\mfailed\M|not enough money|\mno money\M|invalid (stops|volume|request|parameters)|cannot (select|open|create|load)|array out of range|zero divide)' THEN 'error'
 WHEN problem_text ~* '(\m(warn|warning|disconnected|timeout)\M|предупрежден|connection.*lost|no connection|timed out)' THEN 'warning'
 ELSE 'info' END severity
 FROM identities i
), classified AS MATERIALIZED (
 SELECT event_id,received_utc,host_id,terminal_id,stream,raw_line,
 COALESCE(payload_strategy,source_strategy,message_strategy,'__unassigned__') strategy,
 CASE WHEN payload_strategy IS NOT NULL THEN 'payload' WHEN source_strategy IS NOT NULL THEN 'source'
 WHEN message_strategy IS NOT NULL THEN 'prefix' ELSE 'unknown' END strategy_source,
 severity,
 CASE WHEN explicit_level IN ('ERROR','FATAL','CRITICAL','WARN','WARNING','INFO','DEBUG','TRACE') THEN 'payload'
 WHEN mt_level IN ('1','2','3') THEN 'mt5' WHEN severity!='info' THEN 'text' ELSE 'default' END severity_source
 FROM levels
),
