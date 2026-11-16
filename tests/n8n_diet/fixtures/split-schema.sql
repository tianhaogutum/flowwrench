-- Synthetic, hand-authored n8n split-storage interface fixture (MIT).
-- Interface references are recorded in THIRD_PARTY.md; no upstream payloads.
PRAGMA foreign_keys=OFF;
CREATE TABLE workflow_entity (id VARCHAR(36) PRIMARY KEY NOT NULL, name VARCHAR(128) NOT NULL);
CREATE TABLE execution_entity (
 id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL, workflowId VARCHAR(36) NOT NULL,
 finished BOOLEAN NOT NULL DEFAULT 1, mode VARCHAR NOT NULL DEFAULT 'manual',
 startedAt DATETIME, stoppedAt DATETIME, status VARCHAR NOT NULL,
 deletedAt DATETIME(3), createdAt DATETIME(3) NOT NULL DEFAULT '2026-01-01 00:00:00.000',
 storedAt VARCHAR(2) NOT NULL DEFAULT 'db',
 FOREIGN KEY(workflowId) REFERENCES workflow_entity(id) ON DELETE CASCADE
);
CREATE TABLE execution_data (
 executionId INT PRIMARY KEY NOT NULL, workflowData TEXT NOT NULL, data TEXT NOT NULL,
 workflowVersionId VARCHAR(36), FOREIGN KEY(executionId) REFERENCES execution_entity(id) ON DELETE CASCADE
);
CREATE INDEX execution_workflow ON execution_entity(workflowId);
INSERT INTO workflow_entity VALUES ('wf-a','Email'),('wf-b','订单');
INSERT INTO execution_entity(id,workflowId,startedAt,stoppedAt,status) VALUES
 (1,'wf-a','2026-10-01 00:00:00','2026-10-01 00:00:01','success'),
 (2,'wf-a','2026-10-10T00:00:00Z','2026-10-10T00:00:01Z','error'),
 (3,'wf-b','2026-10-25T00:00:00+00:00','2026-10-25T00:00:01+00:00','success'),
 (4,'wf-b','2026-10-31 00:00:00.000','2026-10-31 00:00:01.000','success'),
 (5,'wf-b','2026-10-01 00:00:00',NULL,'running'),
 (6,'wf-a','2026-10-01 00:00:00',NULL,'waiting'),
 (7,'wf-a','2026-10-01 00:00:00',NULL,'success'),
 (8,'wf-b','2026-10-20 00:00:00','2026-10-20 00:00:01','crashed'),
 (9,'wf-b','2026-10-21 00:00:00','2026-10-21 00:00:01','canceled'),
 (10,'missing-workflow','2026-10-01 00:00:00','2026-10-01 00:00:01','unknown');
INSERT INTO execution_data(executionId,workflowData,data) VALUES
 (1,'{}','{"ok":1}'),
 (2,'{}','{"error":"synthetic"}'),
 (3,'{}','{"订单":"你好"}'),
 (4,'{}','[1,2,3]'),
 (5,'{}','{}'),
 (6,'{}','{}'),
 (8,'{}','null'),
 (9,'{}','false'),
 (999,'{}','{"orphan":true}');
