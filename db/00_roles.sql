-- Roles must exist before ANY grants run anywhere.
-- Previously these lived in 02_security.sql, but 01_schema.sql grants to
-- these roles before 02 runs — so a fresh `docker compose down -v && up`
-- failed at 01 and left an empty database.

CREATE ROLE candidate_role NOLOGIN;
CREATE ROLE judge_role     NOLOGIN;
CREATE ROLE worker_role    NOLOGIN;
CREATE ROLE admin_role     NOLOGIN;
