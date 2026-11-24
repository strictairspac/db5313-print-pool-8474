# 初始结构

`src/models/tables.py` 定义初始关系结构。API 启动时显式创建演示库的表；此版本只包含初始 schema。需要在已有部署升级表结构时，应单独引入版本化迁移。

## 备份恢复变更

误删办结单恢复功能引入以下结构变更，全新部署由 `schema.create_all` 直接建出；已有部署需手动执行：

```sql
CREATE TABLE business_backups (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id INTEGER NOT NULL UNIQUE,
    case_id INTEGER NOT NULL,
    run_snapshot JSON NOT NULL,
    evidence_snapshot JSON NOT NULL,
    created_at VARCHAR(32) NOT NULL
);
CREATE INDEX ix_business_backups_run_id ON business_backups (run_id);
```

- 执行单与凭证按原编号显式恢复后，需把序列推到该编号之后：
  `SELECT setval(pg_get_serial_sequence('business_runs','id'), :run_id, true);`
  凭证表同理。
- 新增数据库值班员角色 `duty`，演示环境由 `scripts/seed_demo.py` 写入 `accounts`；
  已有环境需自行插入账号并分配 `duty` 角色。
