---
name: Friday
description: 延续纸张与哑光石墨的家庭操作界面与纸艺游戏世界
colors:
  accent: "#424449"
  accent-hover: "#292b2f"
  accent-soft: "#e6e6e3"
  bg: "#f5f5f3"
  sidebar: "#e9e9e6"
  surface: "#ffffff"
  ink: "#292a2c"
  muted: "#606165"
  line: "#dededb"
  warn: "#875b16"
  warn-bg: "#fff4dc"
  bad: "#a23340"
  bad-bg: "#fff0f1"
  good: "#23684d"
  good-bg: "#e9f5ee"
  focus: "#424449"
  game-ink: "#2d2e30"
  game-muted: "#686962"
  game-paper: "#f7f6f1"
  game-line: "#d7d7cf"
  game-control: "#fafaf6"
  game-control-hover: "#efefe8"
  game-hud: "#faf9f2ef"
  game-hud-line: "#e1e0d7"
typography:
  headline:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'PingFang SC', 'Segoe UI', sans-serif"
    fontSize: "1.75rem"
    fontWeight: 650
    lineHeight: 1.3
    letterSpacing: "-.025em"
  title:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'PingFang SC', 'Segoe UI', sans-serif"
    fontSize: "1.125rem"
    fontWeight: 600
    lineHeight: 1.45
    letterSpacing: "-.015em"
  body:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'PingFang SC', 'Segoe UI', sans-serif"
    fontSize: ".9375rem"
    fontWeight: 400
    lineHeight: 1.55
  control:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'PingFang SC', 'Segoe UI', sans-serif"
    fontSize: ".875rem"
    fontWeight: 400
    lineHeight: 1.55
  caption:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'PingFang SC', 'Segoe UI', sans-serif"
    fontSize: ".8125rem"
    fontWeight: 400
    lineHeight: 1.55
  game-mission:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'PingFang SC', sans-serif"
    fontSize: "22px"
    fontWeight: 650
    lineHeight: 1.3
    letterSpacing: "-.025em"
  game-instruction:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'PingFang SC', sans-serif"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.6
  game-progress:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'PingFang SC', sans-serif"
    fontSize: "15px"
    fontWeight: 650
    lineHeight: 1.4
rounded:
  compact: "6px"
  control: "8px"
  stage-control: "10px"
  surface: "12px"
  dialog: "16px"
  circle: "50%"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  section: "24px"
  workspace: "32px"
components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.surface}"
    typography: "{typography.control}"
    rounded: "{rounded.control}"
    padding: "9px 13px"
  button-primary-hover:
    backgroundColor: "{colors.accent-hover}"
  button-secondary:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    typography: "{typography.control}"
    rounded: "{rounded.control}"
    padding: "9px 13px"
  button-secondary-hover:
    backgroundColor: "{colors.accent-soft}"
  button-text:
    backgroundColor: "transparent"
    textColor: "{colors.accent}"
    typography: "{typography.control}"
    rounded: "{rounded.control}"
    padding: "9px 6px"
  field-search:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    typography: "{typography.control}"
    rounded: "{rounded.control}"
    padding: "8px 12px"
    width: "220px"
  navigation-item:
    backgroundColor: "transparent"
    textColor: "#5d5f63"
    typography: "{typography.control}"
    rounded: "{rounded.control}"
    padding: "11px 14px"
  navigation-item-current:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.accent}"
  status-good:
    backgroundColor: "{colors.good-bg}"
    textColor: "{colors.good}"
    rounded: "{rounded.compact}"
    padding: "3px 9px"
  inspector:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.surface}"
    padding: "22px"
  device-row:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    padding: "10px 12px"
  game-button-primary:
    backgroundColor: "{colors.game-ink}"
    textColor: "{colors.surface}"
    rounded: "{rounded.stage-control}"
    padding: "10px 15px"
  game-live-progress:
    backgroundColor: "{colors.game-paper}"
    textColor: "{colors.game-ink}"
    typography: "{typography.game-progress}"
    rounded: "{rounded.compact}"
    padding: "4px 7px"
---

# Design System: Friday

## Overview

**Creative North Star: "纸张与哑光石墨"**

Friday 沿用现有 Icon 的折纸、典雅深灰与哑光金属质感。日常 Operate 界面以中性纸面承接内容，以石墨文字、细分隔线和熟悉的系统控件组织操作。密度服务于读状态和采取下一步动作；原因、原始异常与诊断保留在按需展开的区域，日常页面保持简洁。

Games 是同一身份中的 Experience：已有纸艺、黏土插画构成封面与游戏世界，操作层继续使用克制的纸面和深色控件。插画承担场景、材质与角色表达，状态文字和生命周期操作保持清楚；既有游戏美术不扩展为日常操作页的装饰主题。macOS 外壳保留原生标题栏、系统文字角色、进度和授权控件。

**Key Characteristics:**

- 原 Friday Icon 与现有插画、Godot 手势源各自保留其身份和来源。
- 中性纸面与石墨构成日常工作区，语义颜色只标识实际状态。
- 操作靠近其对象，行、分隔线和按需详情承担组织结构。
- 游戏世界领先，任务、实底反馈和稳定操作区支持观看与游玩。

本记录依据当前源码 `095406ed1d2926c569c8858be754d9cceebe2b93` 的实际 CSS、组件及已确认的两个 surface contract。前置 token 是该实现的机器可读摘录；圆角、间距与排版角色的键是文档名称，未向产品新增 CSS 变量。`game-` 前缀区分游戏页面自身的基础值，不把两个页面中同名的 CSS 变量合并。具体 surface 的结构仍以各自 contract 为准。

本次视觉 ship 仅覆盖原有六项修复，依据的截图来自隔离合成环境。设计记录不扩大为完整功能、语音或动作识别准确性、已安装 App、真实设备或现场验收。公开视觉依据为 `PRODUCT.md`、`.impeccable/surfaces/homeplane-static-index-html.md` 与 `.impeccable/surfaces/embodied-games-html.md`；具体样式以 `homeplane/static/style.css`、`homeplane/static/interface.css`、`homeplane/static/games-library.css` 和 `homeplane/embodied/static/games.css` 为准。

## Colors

配色沿用实际实现：日常界面接近中性的纸白、浅灰与石墨，游戏舞台的纸面稍暖，插画自身保留已批准的场景色彩。

### Primary

- **石墨操作色**（`accent` / `accent-hover`）：主要按钮、链接和关键选中状态；悬停使用已有深一级值。
- **浅石墨底**（`accent-soft`）：轻选中、筛选和次要悬停的衬底。
- **焦点石墨**（`focus`）：键盘焦点轮廓；与主要操作色同源。

### Secondary

- 这一组是语义状态色，不能延伸为第二套品牌配色。
- **正常绿**（`good` / `good-bg`）：已具备证据的健康、计划等状态。
- **关注褐金**（`warn` / `warn-bg`）：过期、未知、等待核实或需要关注的状态。
- **失败红**（`bad` / `bad-bg`）：失败、拒绝、失效及相应恢复提示。

### Neutral

- **中性纸面**（`bg`）、**侧栏灰**（`sidebar`）、**白色内容面**（`surface`）：组织应用、导航与局部详情，不给每个信息块重复包卡。
- **正文石墨**（`ink`）、**次级灰**（`muted`）、**细分隔灰**（`line`）：建立阅读层次与相邻区域的边界。
- **游戏纸面**（`game-paper`）、**游戏石墨**（`game-ink`）、**游戏次级灰**（`game-muted`）、**游戏分隔灰**（`game-line`）：仅对应独立舞台的基础上下文。
- **游戏控件面**（`game-control` / `game-control-hover`）与 **HUD 纸面**（`game-hud` / `game-hud-line`）：分别用于舞台按钮和任务、计分容器。HUD 带既有轻透明度；必要次数和保持反馈使用实底游戏纸面。
- 高对比模式沿用现有 CSS 对次级文字与分隔线的覆盖值；它们是辅助功能分支，不替换默认主题。

**The 语义着色 Rule.** 色彩跟随实际状态，并保留文字说明；未知结果不借正常色或成功词句显得已经完成。

## Typography

**Body Font:** Web Console 使用系统 UI 字体栈，中文沿用 PingFang SC，其他平台保留现有回退。游戏舞台使用其自身已有的系统字体栈。macOS 外壳使用 SwiftUI 的原生文字角色，不把 Web 的 rem 数值强行换算为原生字号。

**Character:** 以清楚、紧凑的界面文字为主。字重和字号区分页面、分组、操作与辅助信息；未建立独立的品牌展示字体。

### Hierarchy

- **Headline**（`headline`）：页面标题；窄屏沿用当前缩小后的标题。
- **Title**（`title`）：日常工作区的分组标题，保持比正文清楚的一层层次。
- **Body**（`body`）：默认正文；解释性段落沿用工作区已有的行宽限制，主要内容区域最多约（72ch）。
- **Control / Caption**（`control` / `caption`）：按钮和表单、次级状态与测量。窄屏输入框采用当前（1rem）覆盖值。
- **Game Mission / Instruction**（`game-mission` / `game-instruction`）：舞台任务与动作指令；嵌入、手机和低高度横屏均有现成的局部适配。
- **Game Progress**（`game-progress`）：HTML 中的保持时间或完成次数，采用等宽数字。低高度横屏体感布局使用现有（12px）局部值；这不是把文字缩进虚拟画布。
- 计分与资源数字沿用等宽数字特性。商店特色标题、详情标题、倒计时保持其既有局部层级，不将单个场景的强调字号推广为全局字号。

**The 实际字号 Rule.** 必要指令、保持时间和次数由响应式 HTML 承载；不随（1600px）虚拟游戏画布等比例缩小。

## Layout

间距摘录保留现有重复使用的（4 / 8 / 12 / 16 / 24 / 32px）步骤，局部组件仍保留自己的测量。日常桌面侧栏宽度（216px），来源工具条最小高度（56px）；主工作区最大宽度（1480px），桌面内边距为（30px 32px 56px）。首页主区与窄摘要列并排，设备采用房间导航、分组行和相邻详情。

日常布局在（1200px）压紧摘要与设备详情，在（960px）将首页摘要置于下方；到（760px）采用带文字的紧凑导航，并把首页服务、资源和关注事项摘要放到地图前。手机核心入口保留“我的家 / 设备 / 互动控制 / 家庭游戏”，次要目的地通过明确的更多入口访问。导航不是需要横向寻找核心功能的条带。

「家庭感知」与其他服务同列。镜头有中断、正在连接或处理偏慢时，在统计行下放一个可直接打开详情的镜头行。窄屏让两行共用全宽，其他服务继续双列。处理耗时沿用资源区的等宽数字与状态色。

已选用的画面复核和声音复核各在 Friday 栏显示一行。右侧状态可直接打开对应监测设置，复核延迟和资源不足使用关注色。窄屏每项占满一行，状态按钮至少 44px 高。定时读回在原按钮更新文字，该项撤下时焦点回到 Friday 栏标题。

异响详情有已核实的环视画面时，把镜头名、采集时间和查看状态放在同一行。事发时和环视后的画面分组排列，使用等宽数字显示采集时间。宽屏一行可比较三个视角，窄屏每行最多两张，保留原「查看附近录像」入口。

商店使用现有封面网格：通常三列，宽屏四列，窄屏两列；详情随宽度变为纵向。封面和舞台是真实已有美术，尺寸变化保留其适当裁切；不用真实摄像头画面填补环境插画。

游戏舞台沿用已有桌面 HUD、底部反馈与右侧操作簇。手机纵向把任务、世界、反馈和操作排为相邻的明确行，滑雪和网球在实际剩余高度中分配场景（120–380px），反馈至少预留（48px），操作至少预留（44px）。暂停、完成和反馈消失时保留操作锚点。低于（600px）的横屏高度使用现有紧凑布局；体感教学在左右分区保留完整穿衣人物示范，菜单在可用高度内滚动。

狼人杀轮流发言时，公屏以当前发言座位为标题，在原计时位旁提示下一席或投票。座位列表内也标出「发言中」和「下一位」。当前玩家的手机把「发言完毕」作为本环节主操作。主持端的下一位操作与暂停并排。暂停或断线同步时收起发言提示，恢复后从当轮的实际位置继续。

狼人杀结算由获胜阵营主标题和原纸艺封面带出结果。身份按座位排列，复盘按轮选读，放逐与加赛票型各自展开。宽屏并列身份与复盘，窄屏沿同一阅读顺序堆叠，分组用细分隔线。主持操作保留原房间位置。

## Elevation & Depth

日常工作区主要通过纸面色阶、细线和留白分层。设备行不附加悬浮阴影，局部详情保持安静的白色内容面。对话框、浮层反馈和必要菜单才承担额外深度；舞台 HUD 通过纸面和描边衬托文字。现有电源控件的轻微明暗渐变属于控件质感，不推导为整页装饰渐变。

### Shadow Vocabulary

- **对话框面板**（`--shadow-panel`）：实际实现中的柔和面板阴影，完整值位于 sidecar。
- **浮动结果提示**：已有 toast 的局部投影，完整值位于 sidecar；不用于把日常信息行抬成卡片。

**The 平面工作区 Rule.** 先用分组、纸面与细线建立层次；不把浮层投影、局部透明处理或游戏插画扩展为所有页面的装饰结构。

控件颜色过渡沿用（160ms ease-out）；商店封面的悬停缩放沿用（220ms）及原曲线。游戏动作仍由现有规则事件和页面时钟驱动，暂停、恢复和隐藏不自动补播成功反馈。减少动态效果时保留结果和状态、去除非必要空间运动；不新增独立动画循环。

滑雪远山和地平线固定。地面纹理、压雪痕、木门和树随玩家观测到的位置一起推进。两侧装饰树在靠近视野边缘前淡出，让视线集中在前方木门。系统启用减少动态效果时，画面收起雪地细纹和装饰树，仍显示雪道滑痕、碰撞树、木门和终点。

## Shapes

常规控件使用柔和小圆角（`control`），状态标签和局部进度使用更紧凑的圆角（`compact`）。内容面、封面与舞台框使用（`surface`），对话框使用（`dialog`）；游戏按钮沿用其独立圆角（`stage-control`）。圆形电源控件与站点标记保留圆形，图标与文字仍按实际组件对齐。

以细描边和克制的剪裁承接内容，不统一改造成胶囊。品牌图标保留原透明边界和完整比例，不重新着色、套底或绘制替代物。教学手型保留已批准 Godot 源模型的平滑石墨轮廓与白色填充，不把被拒绝的自绘轮廓纳入体系。

## Components

### Buttons

清楚、接近系统习惯的动作控件。日常主要、次要与文字按钮沿用前置 component token；普通工作区按钮最小高度（40px），导航、表单和触控关键操作按其当前规则保留更大的命中区域。

主要按钮以石墨和白字建立优先级，次要按钮使用纸白与细边，文字按钮减少表面强调。悬停改变背景与边线；按下、禁用保留实际反馈。Web 焦点为（2px）轮廓及（3px）外偏移；舞台按钮焦点使用其已有（4px）外偏移。舞台按钮桌面至少（42px），手机关键操作至少（44px）。不为示例组件追加产品中不存在的悬停动画或状态。

### Chips

状态标签以紧凑圆角、浅语义底与对应深色字传达状态；不是装饰性的栏目眉题。筛选控件继续使用已有按钮状态：按下时改变底色或使用深色实底，并同步其 pressed 状态。

### Cards / Containers

白色详情面用边界、内边距和标题组织信息，实际设备详情面内边距（22px），圆角使用（`surface`）。日常列表和服务摘要优先使用行与分隔线。封面本身是商店的视觉主体，保留各自既有插画、宽高比、标题和真实人数信息；封面不代表游戏或识别能力已经验收。

### Inputs / Fields

沿用系统熟悉的输入、选择和范围控件。纸白背景、细边和控件圆角统一来自当前实现；标签与输入就近。设备搜索沿用当前宽度与响应式换行，窄屏输入字号使用上述覆盖。占位文字、焦点和禁用使用实际样式；错误与恢复信息在对应动作附近显示。

### Navigation

桌面是带文字与图形图标的侧栏，当前目的地以白面与较深文字标记。手机是可直接发现核心目的地的紧凑布局，更多菜单在附近展开；所有条目保留可读名称与键盘焦点。图标采用已有 SVG 或原生图形，不用字符图标代替。

### Device Rows / Room State

设备以分组行展示名称、文字状态和就近操作；选中详情在相邻区域或窄屏内联出现。首页房间标记包含文字状态摘要和明确房间入口，不以微小模型、颜色或悬停作为唯一状态说明。动作命令、等待 ACK、设备上报和未知结果保持可区分的视觉状态。

摄像头详情让实时画面占据主区，画面下方放查看状态和回看录像。停止查看后收起画面，保留查看与回看入口。巡视进度按已确认的视角计数，和停止入口留在详情。正在查看时，跟随和方向键沿画面排列，窄屏在画面后出现。物理开关和镜头状态共用底部一行。回看页在日期筛选前显示当前镜头名，并提供返回全部录像的入口。列表先占用主区，选中片段后展开播放器。停止回放后焦点回到片段。

感知恢复期间，详情在原画面状态行显示「实时画面 · 感知恢复中」，保留有效预览。无法使用的跟随和巡视控件就地暂停。首页复用「家庭感知」的镜头行。

### Game HUD / Teaching

任务、计分、完整动作示范、保持或次数反馈各有自己的可读区域。必要反馈使用实底 HTML 纸面与深色字，完成后保留预留区域。暂停、结束与更多菜单使用同一操作簇，结算和重玩不把原操作区移到另一侧。教学保留来源动作的成形、保持和复位过程；静态图和本 sidecar 的组件样例均不代替动画或识别验证。

玩家重新入镜后，舞台沿用暂停区显示「重新确认站位」和「站位已确认」，画面保留比分或关卡进度。主要操作是原处的「继续本局」，新 OK 手势的提示和进度放在按钮下方。按键和手势共用三秒倒计时。

## Do's and Don'ts

### Do:

- **Do** 沿用原 Friday Icon、已确认的纸张与哑光石墨身份，以及有来源的现有游戏美术。
- **Do** 把文字状态、相关动作和结果放在同一阅读区域，详细原因按需展开。
- **Do** 在手机直接呈现互动控制和家庭游戏，并保留游戏反馈与操作的相邻关系和稳定锚点。
- **Do** 保留清楚的焦点、减少动态效果分支、合成来源标记和真实能力边界。

### Don't:

- **Don't** 以任意品牌换色、装饰玻璃、重复卡片或全页说明文案替代已确认的操作结构。
- **Don't** 用颜色、微小模型或随画布缩小的文字独自传达必要状态和游戏进度。
- **Don't** 将旧样式中的眉题、字符图标或局部遗留透明处理推广为新的全局规则。
- **Don't** 将原六项视觉修复的 ship、组件样例或合成截图当成整产品、现装 App、识别准确性或真实设备验收。
