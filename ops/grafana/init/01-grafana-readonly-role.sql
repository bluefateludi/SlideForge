-- Grafana 面板专用只读账号（幂等，可重复执行）。
--
-- 已有的 postgres 数据卷不会重跑 initdb 脚本，因此 compose 里用一次性的
-- grafana-db-init 服务在每次 `make up` 时执行本文件；也可以手工执行：
--   docker exec -i ai-ppt-generator-postgres-1 psql -U aippt -d aippt \
--     < ops/grafana/init/01-grafana-readonly-role.sql
--
-- 口令为本地开发固定值（与 compose 里 POSTGRES_PASSWORD 同级别的约定），
-- 仅绑定在 docker 内网，不对外暴露。

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'grafana_ro') THEN
        CREATE ROLE grafana_ro LOGIN PASSWORD 'grafana_ro';
    END IF;
END
$$;

GRANT CONNECT ON DATABASE aippt TO grafana_ro;
GRANT USAGE ON SCHEMA public TO grafana_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana_ro;

-- 之后 Alembic 迁移新建的表也自动对 grafana_ro 可读
ALTER DEFAULT PRIVILEGES FOR ROLE aippt IN SCHEMA public GRANT SELECT ON TABLES TO grafana_ro;
