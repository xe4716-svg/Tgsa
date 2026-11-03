TGMate V2.0 数据库迁移文件

执行顺序：
1. 001_core_foundation.sql
2. 002_broadcast_channels.sql
3. 003_group_moderation.sql

说明：这些文件是数据库迁移稿，执行前请先在测试数据库备份并验证。
不要直接在生产数据库首次执行。需要 PostgreSQL 15+。

文件 SHA-256：
001_core_foundation.sql: b2d4e422e810aaeef41cc5e08907f9492bfd8b8f23bc9bc07f9ba219380264d8
002_broadcast_channels.sql: c96ba56df9d249c0c95ee43e2e8e88457f895443b3cc66676d9fe475ebade11e
003_group_moderation.sql: 4706391c04014cff63b4a13b237e4ff12a3b117d2a9b9cc6162bc62817af5e76
