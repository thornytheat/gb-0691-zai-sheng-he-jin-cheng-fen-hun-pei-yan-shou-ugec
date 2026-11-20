# 接口

`POST /api/run`：JSON中的csv文本，省略使用样例；或multipart字段file。最大5MB。

`POST /api/m1`至`POST /api/m7`：独立运行到指定模块，JSON结构同运行接口。

`GET /api/history`：历史批次。`POST /api/review`接收id、revision和decision（通过或退回）。`GET /api/evidence.csv`提供单一证据导出。

结构错误422，版本变化409，文件过大413；错误返回code与message。所有页面可从左侧七个模块入口访问。

`GET /api/runs/<id>`读取指定批次；页面与证据CSV通过run_id查询参数绑定批次，模块导航保留该参数。

## 复配补料（M7整组补料方案）

`POST /api/reblend/plan`：body含`run_id`，按最新共用库存重算整组补料方案，返回各炉候选材料、最小/最大剂量、补后成分上下界、补后验收、分配原因、库存占用与`plan_hash`/`confirm_key`。

`POST /api/reblend/confirm`：body含`run_id`与可选`confirm_key`。服务端以最新库存重算方案；键不匹配返回409要求刷新。同键重试幂等回放、不二次扣料；无键时按当前最新方案落单。确认在`BEGIN IMMEDIATE`事务内扣减库存并写台账，库存不足整单回滚返回422，绝不超领。

`GET /api/reblend/inventory`：补料材料实时余量、急单交期表与库存台账。`GET /api/reblend/confirmations`与`GET /api/reblend/confirmations/<key>`查询确认单。

`POST /api/reblend/replenish`：body含`material_id`、`change_kg`（正入库负退库）、`note`，退库致负返回422。`POST /api/reblend/reset`：body含`inventory_csv`与可选`orders_csv`，整表重置并留审计记录。

补料库存与交期的种子文件为`sample_data/reblend_inventory.csv`、`sample_data/reblend_orders.csv`，首次访问时自动导入；多炉次演示输入见`sample_data/reblend_demo.csv`。

JSON请求体必须是对象，数组、标量、null、损坏JSON及错误上传字段返回400，且不运行默认样例。
