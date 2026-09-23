SELECT 'CREATE ROLE wardflow_api LOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'wardflow_api')
\gexec

ALTER ROLE wardflow_api WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'api_password';

GRANT CONNECT ON DATABASE wardflow TO wardflow_api;
GRANT USAGE ON SCHEMA ops TO wardflow_api;
GRANT SELECT ON ops.units, ops.beds, ops.visits, ops.kpi_snapshots, ops.processed_events, ops.dead_letter, ops.v_unit_status TO wardflow_api;
