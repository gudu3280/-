# ocsjs 可借鉴功能全量集成清单

> 来源: `ocsjs-4.15.3/packages/scripts/src/projects/cx.ts`
> 目标文件: `11-main/desktop/core/chaoxing.py`

---

## 状态说明

- ✅ 已完成 — 代码已集成
- 🔲 待实现 — 需要新增代码
- ⏭️ 跳过 — 不适用或已有等价实现

---

## 一、任务点完成状态检测（已完成）

### 1.1 window.attachments API 集成 ✅
- **ocsjs 位置**: L68-86, L1529
- **实现**: `get_task_points()` extract_js 中读取 `_win.attachments`，通过 `_jobid` 匹配获取 `is_job`/`is_passed`
- **实现**: `_batch_recheck_all_tasks()` 策略0 - attachments API 批量检查
- **实现**: `_recheck_single_task_finished()` 策略0 - attachments API 精准检查

### 1.2 ans-job-finished DOM 类名检测 ✅
- **ocsjs 位置**: L786-793
- **实现**: extract_js 第一轮 `.ans-job-icon` 遍历

### 1.3 jobUnfinishCount 计数器 ✅
- **ocsjs 位置**: L1138-1144
- **实现**: 批量重检 + 主文档补充检查

---

## 二、视频播放增强

### 2.1 视频加载错误检测 + 自动跳过 ✅
- **ocsjs 位置**: L1742-1753
- **原理**: 每3秒检测 `.vjs-modal-dialog-content` 中的错误文案
- **错误关键词**:
  - "视频文件损坏"
  - "网络错误导致视频下载中途失败"
  - "视频因格式不支持"
  - "网络的问题无法加载"
- **行为**: 检测到后 log 错误，3秒后 resolve（跳过该视频）
- **实现位置**: `play_video()` 的视频监控循环中，每轮检查前增加错误检测
- **参考代码**:
```javascript
const errorDiv = doc.querySelector('.vjs-modal-dialog-content');
if (['视频文件损坏', '网络错误导致视频下载中途失败', 
     '视频因格式不支持', '网络的问题无法加载']
    .some(s => errorDiv?.innerText.includes(s))) {
    // 跳过视频
}
```

### 2.2 人脸识别检测 + 等待 ✅
- **ocsjs 位置**: L2221-2300
- **旧版检测**: `#fcqrimg` 元素的 `src` 属性非空时激活
- **新版检测**: `.chapterVideoFaceMaskDiv` 的 `display !== 'none'` 时激活
- **行为**: 检测到人脸识别 → 暂停播放 → 每3秒轮询 → 人脸消失后恢复播放 → 日志通知
- **实现位置**: `play_video()` 的播放循环中，暂停检测之前增加人脸检测
- **参考代码**:
```javascript
function hasFaceRecognition() {
    const faces = document.querySelectorAll('#fcqrimg');
    for (const face of faces) {
        if (face.getAttribute('src')) return true;
    }
    return false;
}
function hasNewFaceRecognition() {
    const faces = document.querySelectorAll('.chapterVideoFaceMaskDiv');
    for (const face of faces) {
        if (face.style.display !== 'none') return true;
    }
    return false;
}
```

### 2.3 视频内弹出题目自动作答 ✅
- **ocsjs 位置**: L1710-1734
- **原理**: 视频播放过程中会弹出 `#videoquiz-submit` 按钮，需要作答后才能继续
- **行为**:
  1. 每3秒检测 `#videoquiz-submit` 按钮是否出现
  2. 出现时随机选择一个 `.ans-videoquiz-opt label` 选项
  3. 点击选项 → 点击提交按钮
  4. 等待3秒 → 隐藏题目元素 `#video .ans-videoquiz` 和 `.x-component-default`
  5. 继续播放
- **实现位置**: `play_video()` 的播放循环中，增加视频内题目检测
- **注意**: 这个检测需要在 video iframe 内执行，不是 cards iframe

### 2.4 视频进度条固定显示 ⏭️
- **ocsjs 位置**: L1666-1673
- **原理**: 设置 `.vjs-control-bar` 的 `opacity = '1'` 使其始终可见
- **说明**: 我们的桌面端方案通过直接操控视频元素，不需要此UI优化

---

## 三、闯关/解锁模式处理

### 3.1 闯关模式检测 ✅
- **ocsjs 位置**: L1087-1089
- **原理**: 检测侧边栏中是否存在 `.catalog_points_sa` 或 `.catalog_points_er` 元素
- **行为**: 如果检测到闯关模式，在日志中提示用户
- **实现位置**: `process_chapter()` 开始处理前检查

### 3.2 闯关模式卡死检测 ✅
- **ocsjs 位置**: L1091-1113
- **原理**: 同一章节连续进入3次 → 判定为卡住（可能是章节测试未完成）
- **行为**: 弹窗提醒用户手动完成章节测试
- **实现**: 维护一个 `{chapter_id: count}` 的计数器，进入章节时+1，达到3次时告警
- **实现位置**: `process_chapter()` 入口处

### 3.3 当前章节完成状态检测 ✅
- **ocsjs 位置**: L1185-1193
- **原理**: 检测 `.posCatalog_active` 内是否有 `.icon_Completed`
- **行为**: 已完成则跳过该章节，直接进入下一章
- **实现位置**: `process_chapter()` 任务循环之前检查

---

## 四、章节测试增强

### 4.1 章节测试已完成检测（跳过答题）✅
- **ocsjs 位置**: L1568-1574
- **原理**: 检测 `.testTit_status` 元素是否包含 `.testTit_status_complete` 类
- **行为**: 如果章节测试已完成，log 提示并跳过
- **实现位置**: 处理章节测试类型任务点时，先检测完成状态

### 4.2 答题失败时随机作答 ✅
- **ocsjs 位置**: L2004-2044
- **原理**: 当搜题无结果时：
  - 选择题：随机点击一个选项
  - 填空题：从预设文案库中随机选一个填入
- **行为**: 避免题目卡住导致后续题目无法进行
- **实现位置**: 答题结果处理逻辑中，对 `finish=false` 的题目增加随机作答

---

## 五、PPT/书籍/阅读任务

### 5.1 PPT 任务完成（finishJob）✅
- **ocsjs 位置**: L1794-1798
- **原理**: 普通阅读任务点调用 `win.finishJob()` 直接完成
- **行为**: 在 PDF/书籍 iframe 中查找 `finishJob` 函数并调用
- **实现位置**: 处理 `pdf` 类型任务时

### 5.2 带音频 PPT 任务（swiperNext）✅
- **ocsjs 位置**: L2127-2142
- **原理**: 遍历 `.swiper-slide`，逐页调用 `win.swiperNext()` 翻页
- **行为**: 静音音频 + 逐页翻阅 + 等待完成
- **检测**: `.swiper-container` 存在时触发
- **实现位置**: 处理 PPT 类型任务时

### 5.3 定时阅读任务（2026新版）✅
- **ocsjs 位置**: L338-376, L1802-1813
- **URL**: `/readsvr/book/mooc`
- **原理**: 
  1. 等待 `timing` 参数指定的秒数 + 3秒
  2. 跳转到正文页（`jumper.value = '5'`）
  3. 再等同样时间
  4. 跳转到封底页（`jumper.value = '7'`）
  5. 点击 `.readerPager` 元素完成任务
- **实现位置**: 新增阅读任务处理方法

### 5.4 书籍阅读跳末页 ✅
- **ocsjs 位置**: L377-384
- **原理**: 对于 `#ReadWeb` 元素存在的普通书籍，调用 `readweb.goto(epage)` 跳到末页
- **行为**: 等待5秒后执行跳转
- **实现位置**: 书籍类型任务处理

---

## 六、链接任务

### 6.1 链接任务点完成 ✅
- **ocsjs 位置**: L2146-2155
- **检测**: iframe 中存在 `#hyperlink` 元素
- **原理**:
  1. 保存原始 onclick
  2. 设置 onclick 返回 false（阻止弹窗）
  3. 调用 click()
  4. 还原 onclick
- **行为**: 自动完成链接任务点，不弹出新窗口
- **实现位置**: 任务类型检测和分发逻辑中

---

## 七、繁体字/加密字体解密

### 7.1 font-cxsecret 字体解密 ✅
- **ocsjs 位置**: L994-1067
- **原理**:
  1. 检测页面 `<style>` 中是否包含 `font-cxsecret`
  2. 提取 base64 编码的字体数据
  3. 用 Typr.js 解析字体文件
  4. 遍历 Unicode 中文范围 [19968, 40870]
  5. 对每个字符: `codeToGlyph` → `glyphToPath` → `MD5(path).slice(24)` 得到8位hex
  6. 用 hex 在 `ttf_table.json` 中查找对应的真实 Unicode 码点
  7. 替换 DOM 中 `.font-cxsecret` 元素的 innerHTML
- **我们的情况**: 已有 `font_decrypt.py` 和 `ttf_table.json`，完整实现含主页面解密和 iframe 解密
- **状态**: ✅ 已实现（FontDecryptor 类，257行）
- **实现位置**: 答题前/提取题目时执行

### 7.2 判断题繁体字转换 ✅
- **ocsjs 位置**: L2052-2076
- **原理**: 判断题选项可能是图片而非文字，需要将图片转换为 √/× 文字
  - `True` → `√`, `False` → `x`
  - `對` → `√`, `錯` → `x`
  - 检测 `.ri` class 判断对错（ri = right icon = √，无 ri = ×）
- **实现位置**: 答题选项预处理阶段

---

## 八、页面导航与兼容性

### 8.1 旧版→新版自动重定向 ✅
- **ocsjs 位置**: L514-561
- **检测 URL 特征**:
  - `mooc2=0`
  - `studentcourse`（旧版路径）
  - `work/getAllWork`
  - `work/doHomeWorkNew`
  - `exam/test?`
- **行为**: 设置 `mooc2=1` + `newMooc=true`，执行 `window.location.replace()`
- **实现位置**: 页面加载后/章节导航时

### 8.2 任务页面→章节页面重定向 ✅
- **ocsjs 位置**: L496-513
- **检测**: `pageHeader=0` 参数
- **行为**: 点击 `a[title="章节"]` 切换到章节列表
- **实现位置**: 进入课程后检查

### 8.3 top 窗口定位（跨域处理）⏭️
- **ocsjs 位置**: L137-156
- **原理**: 向上遍历 parent 窗口，找到包含 `/mycourse/studentstudy` 的真正 top
- **行为**: 解决跨域 iframe 导致 top 指向外层壳页面的问题
- **我们的情况**: 使用 Playwright 直接操控 tab，可能不需要此逻辑
- **评估**: ⏭️ 可能不需要（Playwright 直接操作页面，不走 iframe 嵌套）

### 8.4 多域名支持 ⏭️
- **ocsjs 位置**: L95-121
- **域名列表**: `chaoxing.com`, `edu.cn`, `org.cn`, `xueyinonline.com`, `hnsyu.net`, `sslibrary.com`, `xuexi365.com` 等20+
- **我们的情况**: 大部分用户用主域名，可后续扩展
- **优先级**: 低

### 8.5 自动寻找未完成的第一个章节 ⏭️
- **ocsjs 位置**: L686-702
- **原理**: 过滤 `unFinishCount !== 0` 的章节，用 `getTeacherAjax(courseId, classId, chapterId)` 跳转
- **我们的情况**: 已有类似的未完成任务跳过逻辑，可对比优化
- **实现位置**: 课程学习开始前

---

## 九、编辑器增强

### 9.1 复制粘贴限制解除 ✅
- **ocsjs 位置**: L603-665
- **原理**: 移除 UE 编辑器上的 `beforepaste` 事件监听
  - `ue.removeListener('beforepaste', editorPaste)`
  - `ue.removeListener('beforepaste', myEditor_paste)`
- **行为**: 允许在超星编辑器中粘贴内容
- **实现位置**: 答题/提交时如遇到编辑器限制

---

## 十、任务调度优化

### 10.1 任务去重（mid 唯一标识）✅
- **ocsjs 位置**: L1525
- **原理**: 通过 `attachment.property.mid` 去重，避免同一任务点被重复处理
- **行为**: `searchedJobs.find(job => job.mid === attachment.property.mid) === undefined` 时才处理
- **实现位置**: `process_chapter()` 的任务循环中

### 10.2 递归搜索 iframe ⏭️
- **ocsjs 位置**: L1452-1470
- **原理**: BFS 遍历所有嵌套 iframe（不仅仅是第一层）
- **行为**: `searchIFrame(root)` 返回所有可达的 iframe 列表
- **我们的情况**: 已有 `_eval_in_iframe` 和多层嵌套处理，可能已覆盖
- **评估**: 检查是否有遗漏的深层 iframe

### 10.3 附件数量计数超时 ⏭️
- **ocsjs 位置**: L1298-1305
- **原理**: `attachmentCount = window.attachments?.length || 0`，超时 = `3 + count * 2` 秒
- **行为**: 如果10秒内没有新任务点出现，停止搜索
- **我们的情况**: 已有类似超时机制
- **评估**: ⏭️ 已有等价实现

### 10.4 小节位置判断 ⏭️
- **ocsjs 位置**: L1119-1126
- **原理**: 检测 `.prev_ul li` 的最后一个是否有 `.active` 类，判断是否在最后一个小节
- **行为**: 在最后小节时才触发章节切换逻辑
- **实现位置**: 章节切换前判断

### 10.5 章节切换方式（PCount.next）✅
- **ocsjs 位置**: L1411-1436
- **原理**: 调用 `top.PCount.next(count, chapterId, courseId, clazzId, '')` 实现下一节跳转
- **优势**: 比点击按钮更可靠，直接调用超星内部 API
- **实现位置**: 章节切换逻辑中作为补充策略

---

## 十一、考试增强

### 11.1 考试整卷预览重定向 ✅
- **ocsjs 位置**: L562-594
- **检测**: `exam-ans/exam/test/reVersionTestStartNew` 或 `mooc-ans/exam/test/reVersionTestStartNew`
- **行为**: 调用 `top.topreview()` 跳转到整卷预览页面
- **实现位置**: 考试页面处理

### 11.2 禁止整卷预览的考试处理 ✅
- **ocsjs 位置**: L571-589
- **检测**: `.mark_info` 文本包含 "不允许整卷预览"
- **行为**: 改为逐题模式，加快答题速度（缩短间隔到3秒）
- **实现位置**: 考试答题逻辑

---

## 十二、其他

### 12.1 自动滚动到当前活跃章节 ✅
- **ocsjs 位置**: L1146-1150
- **原理**: `document.querySelector('.posCatalog_active').scrollIntoView({ behavior: 'smooth', block: 'center' })`
- **行为**: 侧边栏自动滚动到当前正在处理的章节
- **实现位置**: 章节导航后

### 12.2 等待章节信息加载 ⏭️
- **ocsjs 位置**: L1157-1172
- **原理**: 每秒轮询 `getChapterInfos()`，直到获取到数据或超时（默认10秒）
- **行为**: 确保章节数据加载完成后再开始处理
- **我们的情况**: 已有 sleep + 重试机制
- **评估**: ⏭️ 已有等价实现

### 12.3 高倍速警告 ⏭️
- **ocsjs 位置**: L318-333
- **原理**: 当 playbackRate > 2 时显示警告
- **我们的情况**: 已有倍速探测和降级机制
- **评估**: ⏭️ 已有更好的保护

### 12.4 完成全部后从头重新学习 ⏭️
- **ocsjs 位置**: L242-249, L1357-1361
- **原理**: 到达最后一章后，点击第一个章节名称从头开始
- **实现位置**: 章节循环结束后

---

## 实施优先级排序

### P0 - 必须实现（直接影响功能）
| # | 功能 | 预计改动量 |
|---|------|-----------|
| 2.1 | 视频加载错误检测+跳过 | ✅ 已完成 |
| 2.2 | 人脸识别检测+等待 | ✅ 已完成 |
| 2.3 | 视频内弹出题目自动作答 | ✅ 已完成 |
| 4.1 | 章节测试已完成检测 | ✅ 已完成 |
| 10.1 | 任务去重(mid) | ✅ 已完成 |

### P1 - 高价值（提升稳定性）
| # | 功能 | 预计改动量 |
|---|------|-----------|
| 3.1 | 闯关模式检测 | ✅ 已完成 |
| 3.2 | 闯关卡死检测 | ✅ 已完成 |
| 3.3 | 当前章节完成状态检测 | ✅ 已完成 |
| 7.1 | font-cxsecret 字体解密 | ✅ 已有完整实现 |
| 7.2 | 判断题繁体字转换 | ✅ 已完成 |
| 10.5 | PCount.next 章节切换 | ✅ 已完成 |

### P2 - 中等价值（完善功能覆盖）
| # | 功能 | 预计改动量 |
|---|------|-----------|
| 5.1 | PPT finishJob | ✅ 已完成 |
| 5.2 | 带音频PPT swiperNext | ✅ 已完成 |
| 5.3 | 定时阅读任务 | ✅ 已完成 |
| 6.1 | 链接任务完成 | ✅ 已完成 |
| 4.2 | 答题失败随机作答 | ✅ 已完成 |
| 12.1 | 自动滚动到活跃章节 | ✅ 已完成 |

### P3 - 低优先级（边缘场景）
| # | 功能 | 预计改动量 |
|---|------|-----------|
| 8.1 | 旧版→新版重定向 | ✅ 已完成 |
| 8.2 | 任务页面→章节页面重定向 | ✅ 已完成 |
| 9.1 | 复制粘贴限制解除 | ✅ 已完成 |
| 10.4 | 小节位置判断 | ⏭️ 跳过 |
| 11.1 | 考试整卷预览重定向 | ✅ 已完成 |
| 11.2 | 禁止整卷预览处理 | ✅ 已完成 |
| 12.4 | 完成后从头重新学习 | ⏭️ 跳过（边缘场景） |

---

## 预估总工作量

- P0: ~145行
- P1: ~150行
- P2: ~130行
- P3: ~110行
- **总计: ~535行新增代码**
