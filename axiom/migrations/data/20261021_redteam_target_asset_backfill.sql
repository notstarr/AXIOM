-- 一次性数据回填（不随 alembic 自动执行）
--
-- 背景：红队成果的「对象」字段（red_team_results.target_asset）此前写的是应用
-- 代号或资产标签（member-management、keewood-console、vke-kube-apiserver…），
-- 控制台「对象」看不出具体目标。按要求改为具体域名 / IP[:端口]（可带括号说明）。
-- 取值来自该条成果自身的 description（curl 目标、回显主机、库名），未做推断。
--
-- 执行前提：20261021_pg0026 已升级（id 已是任务内 x001… 序号）。

WITH mapping(task_id, id, target_asset) AS (
    VALUES
    -- t_230
    ('t_230', 'x001', 'volcengine.com（OpenAPI，账号 2100421404）'),
    ('t_230', 'x002', 'keewood-service.kkgroup.cn（灯塔引擎 console）'),
    ('t_230', 'x003', 'i-magic-service.letsmagic.cn（联系人目录）'),
    ('t_230', 'x004', 'keewood-service.kkgroup.cn（用户组面）'),
    ('t_230', 'x005', '172.29.192.1:443（VKE kube-apiserver）'),
    ('t_230', 'x006', 'performance-management-api.kkguan.com（4C 业绩管理）'),
    ('t_230', 'x007', 'api.kkgroup.cn（KK 开放平台统一认证）'),
    ('t_230', 'x008', 'keewood-service.kkgroup.cn（目标组织应用落库）'),
    ('t_230', 'x009', 'keewood-service.kkgroup.cn（会话令牌直用）'),
    -- t_231
    ('t_231', 'x001', 'teamshare-service.kkguan.com（匿名表单记录）'),
    ('t_231', 'x002', 'teamshare-service.kkguan.com（teamshare-private OSS 任意签名）'),
    ('t_231', 'x003', 'teamshare-service.kkguan.com（SSRF → 云元数据）'),
    ('t_231', 'x004', 'teamshare-service.kkguan.com（TC 门店交易单据 13955 条）'),
    ('t_231', 'x005', 'shk.kkguan.com/universe（actuator 会话泄露）'),
    ('t_231', 'x006', 'shk.kkguan.com/universe（kpos 生产库，宿主 192.168.0.95）'),
    ('t_231', 'x007', 'shk.kkguan.com/universe（kkguan_erp 库）'),
    ('t_231', 'x008', 'shk.kkguan.com/universe（kk_prod_fi 库）'),
    ('t_231', 'x009', 'shk.kkguan.com/universe（kwms 库）'),
    ('t_231', 'x010', '119.23.232.213:8080/prismart/integration'),
    ('t_231', 'x011', '119.23.232.213:8080/prismart（门店后台登录）'),
    ('t_231', 'x012', 'git.kkgroup.cn（GitLab 19.4.2）'),
    ('t_231', 'x013', '119.23.232.213:8080/prismart（商品写落库）'),
    ('t_231', 'x014', '10.192.192.205（GitLab 共享 runner buildhost）'),
    ('t_231', 'x015', 'cosmic.kkguan.com/ierp（金蝶苍穹 kk_prod）'),
    ('t_231', 'x016', '119.23.232.213:8080/prismart/ogi（58 门店价签全量）'),
    ('t_231', 'x017', '192.168.0.95（escheduler SHELL root）'),
    ('t_231', 'x018', '119.23.232.213:8080/prismart（匿名写落库）')
)
UPDATE axiom.red_team_results r
   SET target_asset = m.target_asset
  FROM mapping m
 WHERE r.task_id = m.task_id
   AND r.id = m.id
   AND r.target_asset <> m.target_asset;

-- 回滚：旧值多为域名或应用名（如 keewood-console、vke-kube-apiserver、
-- volcengine-account-2100421404），信息未丢失，见 2026-10-21 交接记录。
