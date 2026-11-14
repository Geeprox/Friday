---
version: 1
slug: "baby-naming"
primary_target: "integrations/baby_naming/web/index.html"
related_targets: ["integrations/baby_naming/web/app.js","integrations/baby_naming/web/style.css","integrations/baby_naming/web/jade-card.js"]
---

# 宝宝起名工作台

模式为 Operate。父母在家庭内网用手机或桌面浏览器异步探索、复看和确认名字。

## Direction contract

THESIS: 每次只处理当前最值得判断的任务，随时可以关掉，换个时间接着做。

OWN-WORLD: 暖白纸面、墨灰文字，松墨绿给「老公」，胭脂红给「老婆」。两枚同笔触人物头像辅助辨识。两套三幅的卡面参考玉雕与中式雕漆，松岩、远山、秋水归青玉系，荷汀、归鹤、疏梅归胭脂系，均用烫金卡边与宋体。暖白纸面让卡片成为焦点。

STORY: 选择身份和男孩或女孩方向，再做一张闪卡、一组有明确目的的对照或一次复看。意见同步后显示共同备选，最终分别确认。

FIRST VIEWPORT: 顶栏返回 Friday，右侧显示当前身份。工作区右上从设置图标进入独立二级页。女孩和男孩用漆色页签切换，姓名用独立的收藏卡；手机是竖构图。首屏中央只突出一个全名或一个对照问题。卡片下方放一两句由来或观察线索，再给出三个操作。手势说明首次使用后消失；桌面右边只放共同备选和个人记录。

FORM: 纸面桌面上的墨玉收藏卡。玉雕与雕漆底纹使用生成素材，Three.js 绘制卡身、斜切边与移动光源；名字与状态是 HTML。素材来源见工作台的 `ARTWORK.md`。

SIGNATURE INTERACTION: 卡边随手指露出厚度，短划或拖回原位后松手会回位。左划淘汰当前名字、下划待定、双击盖下爱心；离场和保存同时开始，先备好下一张，保存后接档，失败恢复原卡。减少动态效果与无 WebGL 时可使用静态卡和同样的按钮。

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
