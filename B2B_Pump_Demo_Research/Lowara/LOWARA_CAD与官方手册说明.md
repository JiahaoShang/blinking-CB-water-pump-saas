# Lowara e-SV — CAD 下载与官方手册获取说明

## 0. 产品用途 / 适用场景

本产品为 e-SV 系列**立式多级离心泵**(与液体接触的全部金属件为不锈钢 AISI 304 / 316;本配置 3SV08NL007T 为 AISI 316 圆法兰 PN 25 版本)。依据厂商技术目录 Rev. B **第 22 页「Typical applications of e-SV series electric pumps」**:

| 领域 | 典型用途 |
|---|---|
| 供水与增压 | 楼宇、酒店、住宅小区的压力增压;增压泵站与供水管网;成套增压机组 |
| 水处理 | 超滤系统、反渗透(RO)系统、软化与除盐、蒸馏系统、过滤 |
| 轻工业 | 清洗与去脂设备(机械零件清洗、洗车/货车清洗线、电子线路板清洗)、商用洗涤设备、消防系统泵 |
| 制药 / 食品饮料 | 有卫生级要求的生产线 |
| 灌溉与农业 | 温室、加湿、喷灌 |
| 暖通空调(HVAC) | 冷却塔与冷却系统、温控系统、制冷、感应加热、换热器、锅炉与热水循环 |

一句话:**这款泵就是给"楼宇/工厂里把清水或工艺水加压、循环、输送"的场景用的**(多级叶轮结构 = 小流量也能打到较高压力),也因此常见于高层供水增压、RO 水处理和锅炉/空调循环。

## 1. CAD 文件(已成功下载)
从 Lowara 官方 CAD 中心(https://cadcenter.lowara.com/en/series.php?cf=35)选择的具体配置:

| 项目 | 值 |
|---|---|
| **产品型号** | **3SV08NL007T** |
| 安装形式 | Complete Pump(整泵) |
| 规格 Size | 3SV |
| 极数 Poles | 2 |
| 相数 Phase | 3 |
| 额定功率 | 0.75 kW |
| 电机 | Lowara SM |

已下载文件:
- `LOWARA_e-SV_3SV08NL007T_2D_DXF.zip` — 2D 图纸(DXF,压缩包)
  原始地址:https://cad.lowara.com/dxf/e-SV/2-Pole/Dxf/3Sv/3SV-FL/3sv08fl007t-d_a_dd.zip
- `LOWARA_e-SV_3SV08NL007T_3D_STEP.zip` — 3D 模型(STEP,压缩包)
  原始地址:https://cad.lowara.com/dxf/e-SV/2-Pole/Step/3Sv/3SV-FL/3sv08fl007t-d_a_3d.zip
- `LOWARA_e-SV_3SV08NL007T_2D预览.png` / `LOWARA_e-SV_3SV08NL007T_3D预览.html.gz` — 预览文件

> ⚠️ 文件名映射说明:CAD 文件命名使用 **3sv08fl007t**(FL = Lowara SM 电机代号),
> 与产品列表中显示的 **3SV08NL007T**(NL 代号)不同。该映射来自厂商 CAD 中心页面本身,
> 我们未做任何改写。若用于对外材料,请以厂商页面为准核对。

## 2. 技术目录(已获取的版本)
| 文件 | 版本 | 来源 |
|---|---|---|
| `Lowara_e-SV_技术手册_50Hz_镜像brownbros_revB.pdf` | Rev. B(2012 版) | 公开镜像(brownbros.co.nz) |
| `Lowara_e-SV_技术手册_50Hz_镜像lenntech.pdf` | 旧版(Ed. 版次见首页) | 公开镜像(lenntech.com) |

官方现行版本为 **Cod. 191002071 Rev. M Ed.01/2024**(官网地址见下),**本次未能下载**:
https://www.xylem.com/siteassets/brand/lowara/resources/technical-brochure/191002071_m_01-2024_esv-50hz_en.pdf
原因:Xylem/Cloudflare 对该 PDF 的自动化访问返回 403(HTML 挑战页),经 curl 多种请求头组合与真实浏览器直取均被拦截。
按你的要求,**未采取绕过限制的手段**;如需现行版,可在浏览器中人工下载(通常可正常打开)。

## 3. 产品页文本
`_xylem_productpage_text.txt` — Xylem 官网 e-SV 产品页抓取文本(可供查阅)

## 4. 被拦截证据
- `_证据_官方PDF被Cloudflare拦截403.html` — 官方 PDF 返回的 403 页面
- `_证据_产品页被拦截403.html` — 产品页 curl 访问返回的 403 页面
