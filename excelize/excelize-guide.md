# Excelize 技术指南

> 服务端生成 Excel 的难点不在 API，而在内存：10 万行用普通 API 会吃掉几个 GB，流式写入才能把峰值压到常量级。
> 上游：<https://github.com/qax-os/excelize> **v2.9.x**　|　文档：<https://xuri.me/excelize/>　|　许可：BSD-3-Clause

---

## 1. 项目简介

`excelize` 是 Go 生态里最主流的电子表格读写库，纯 Go 实现、无 CGO、无外部 Office 依赖，直接按 ECMA-376 / ISO-29500（OOXML）标准操作 `.xlsx` / `.xlsm` / `.xltx` / `.xlam` 等格式。

它解决的问题很具体：**在服务端把结构化的数据变成一份人类能看的表格，或者把用户上传的表格解析成结构化数据**。典型场景是报表导出、对账单生成、批量导入校验、财务模板填充。

核心特征：

- **完整的 OOXML 支持**：样式、合并单元格、条件格式、数据验证、图表、图片、数据透视表、切片器；
- **双写入通道**：普通 API（随机读写，内存换便利）与 StreamWriter（顺序写，常量内存）；
- **公式引擎**：`CalcCellValue` 可对 400+ 内置函数求值，不依赖 Excel 客户端；
- **跨语言绑定**：同仓库提供 `excelize-wasm`（浏览器/Node）、PyPI 与 .NET 包。

一句话定位：**它不是"操作 Excel 的驱动"，而是一个 OOXML 文档处理器**——每个 API 最终都落到某个 XML 部件的某个节点上。

### 安装

```bash
go get github.com/xuri/excelize/v2@v2.9.1
```

包名仍是 `excelize`，但模块路径带 `/v2`。v2.9.1 起要求 Go 1.23+，老项目请锁版本或升级工具链。

---

## 2. 能力总览

| 能力域 | 主要 API | 说明 |
|---|---|---|
| 工作簿 | `NewFile` / `OpenFile` / `OpenReader` / `SaveAs` / `Write` / `Close` | `Write(io.Writer)` 可直接写 HTTP 响应，免落盘 |
| 工作表 | `NewSheet` / `DeleteSheet` / `SetSheetName` / `MoveSheet` / `GetSheetList` | `MoveSheet` 为 v2.9 新增 |
| 单元格写入 | `SetCellValue` / `SetCellInt` / `SetCellFloat` / `SetCellBool` / `SetCellStr` / `SetSheetRow` | 强类型入口能避免"数字变文本" |
| 单元格读取 | `GetCellValue` / `GetCellType` / `GetRows` / `Rows`（迭代器） | 大文件必须用迭代器 |
| 流式写入 | `NewStreamWriter` / `SetRow` / `SetColWidth` / `MergeCell` / `AddTable` / `Flush` | 顺序写，峰值内存与行数无关 |
| 样式 | `NewStyle` / `SetCellStyle` / `GetStyle` | 样式是共享池，按 ID 引用 |
| 布局 | `MergeCell` / `SetColWidth` / `SetRowHeight` / `SetPanes` | 冻结窗格靠 `SetPanes` |
| 公式 | `SetCellFormula` / `GetCellFormula` / `CalcCellValue` | 计算结果不会自动回写单元格 |
| 图表 | `AddChart` / `AddChartSheet` | 支持柱/线/饼/散点/股价等，v2.9 起可组合图 |
| 图片 | `AddPicture` / `GetPictures` / `DeletePicture` | 支持单元格锚点与绝对锚点 |
| 校验与格式 | `AddDataValidation` / `AddConditionalFormat` / `AddIgnoredErrors` | v2.9 起部分函数并发安全 |
| 其他 | `AddPivotTable` / `GetSlicers` / `AddComment` / `SetDocProps` | 透视表、切片器、批注、文档属性 |

---

## 3. 核心概念与原理

### 3.1 一个 xlsx 就是一个 zip

`.xlsx` 没有二进制魔数结构，它就是个 zip 包（这也是为什么"改扩展名成 zip 能解开"）。解开后大致是：

```text
xl/
├── workbook.xml          # 工作表清单、定义名称、计算属性
├── worksheets/sheet1.xml # 真正存单元格的部件，一行一个 <row>
├── sharedStrings.xml     # 全簿共享的字符串池
├── styles.xml            # 字体/填充/边框/数字格式，按 xf 索引引用
├── charts/chart1.xml     # AddChart 生成的图表定义
├── drawings/drawing1.xml # 图表与图片的锚点信息
└── media/image1.png
```

关键点：**单元格里存的是"索引"而不是完整内容**。字符串存 `t="s"` + `sharedStrings` 下标，样式存 `s="3"` 指向 `styles.xml` 的第 3 个 `cellXfs`。所以体积与内存本质上由"共享表规模"和"XML 节点数"决定。

### 3.2 共享字符串表：内存的真正账本

普通 API 写入 `SetCellValue("A1", "华东区")` 时，excelize 会把字符串塞进内存里的 `sharedStrings` 池：重复值只存一份，但**池子随唯一值数量线性增长**，且全部驻留内存直到 `Save`。

- 10 万行 × 10 列如果都是唯一订单号，池子里就有 100 万个条目；
- 每条要存 Go string + XML 转义副本 + map 索引，实测量级是每单元格几百字节；
- 结论：**数据越"不重复"，普通 API 越吃内存**，这就是"10 万行导出 OOM"的根因。

StreamWriter 走另一条路：**字符串直接内联写进 sheet XML**（`t="inlineStr"`），不建共享池，落盘即释放。代价是不再压缩重复值，文件可能略大，但内存是常量级。

### 3.3 两套写入 API 的取舍

| 维度 | 普通 API（`SetCellValue`） | 流式 API（`NewStreamWriter`） |
|---|---|---|
| 写入顺序 | 任意，可回头改 | **必须行号递增**，写完不能回改 |
| 内存 | 与数据量线性相关 | 常量（约几十 MB），超 16MB 自动溢写临时文件 |
| 随机读取 | 支持 | **不支持**，写入期拿不到单元格值 |
| 样式 | 任意单元格独立设置 | 整行或整列设置（`RowOpts` / `SetColWidth`） |
| 适用 | < 1 万行、需要回填合计行 | 万行以上、纯追加导出 |

硬规则：**同一张工作表里，普通函数和流式函数不能混用**。`NewStreamWriter` 接管后再调 `SetCellValue` 写同一 sheet，轻则丢数据，重则产出的文件被 Excel 判定损坏。

### 3.4 样式是共享池，不是单元格属性

`NewStyle` 返回的是 `int` 索引，指向 `styles.xml` 的 `cellXfs` 数组。每次调用都会**新增**一个条目，excelize 内部不做去重。

```go
// 反例：在 10 万行的循环里调用，styles.xml 会膨胀到几十 MB
for i := 2; i <= 100000; i++ {
    id, _ := f.NewStyle(&excelize.Style{Font: &excelize.Font{Bold: true}})
    f.SetCellStyle("Sheet1", fmt.Sprintf("A%d", i), fmt.Sprintf("A%d", i), id)
}
```

正确做法是**在循环外建好样式，循环内复用同一个 ID**。`Style` 结构由 `Font`、`Fill`、`Border`、`Alignment`、`Protection`、`NumFmt` 六部分组成，未设置的字段继承默认值；`CustomNumFmt` 用于自定义数字格式（如 `#,##0.00_);[Red](#,##0.00)`）。

### 3.5 读路径：迭代器优先

`GetRows` 会把整张表读成 `[][]string` 返回——等于把"XML 解析 + 字符串解码 + 切片扩容"三份开销同时压在内存里。`Rows` 迭代器则是流式解析 `sheetN.xml`，每次只保留当前行：

```go
rows, err := f.Rows("Sheet1") // 迭代器，而不是 GetRows
if err != nil {
    return err
}
defer rows.Close()
for rows.Next() {
    cols, err := rows.Columns() // 只解析当前行
    if err != nil {
        return err
    }
    _ = cols
}
```

v2.9.1 对 `Rows` 提速约 20%、内存下降约 10%，v2.10 又把 `GetSheetDimension` 改为流式解析（提速约 95%）。**判断标准：文件超过 10MB 或行数未知，一律用迭代器。**

### 3.6 并发模型：File 不是并发安全的

`*excelize.File` 内部有 `sync.Mutex` 保护部分写入路径，但**不保证跨 goroutine 的复合操作安全**；v2.9 仅把 `AddDataValidation`、`DeleteDataValidation`、`GetCellStyle` 少数函数标注为并发安全。

实践结论：

- **禁止多个 goroutine 写同一个 `File`**（尤其同一个 sheet），会出现 XML 结构错乱；
- 正确姿势是**一个 goroutine 一个 File**，或按 sheet 拆分后各自导出再合并；
- 多 sheet 并行收益远小于调试成本，优先单 worker 串行写。

---

## 4. 使用实战

### 4.1 十万行导出：StreamWriter

这是最该背下来的一段代码。要点：样式只建一次、列宽在写行之前设置、行号递增、最后必须 `Flush`。

```go
package main

import (
    "fmt"
    "math/rand"
    "strconv"

    "github.com/xuri/excelize/v2"
)

func exportLarge(path string, total int) error {
    f := excelize.NewFile()
    defer f.Close()

    const sheet = "订单明细"
    if err := f.SetSheetName("Sheet1", sheet); err != nil {
        return err
    }

    // 1) 样式在流式写入之前建好，全程复用同一个 ID
    headerStyle, err := f.NewStyle(&excelize.Style{
        Font:      &excelize.Font{Bold: true, Color: "FFFFFF"},
        Fill:      excelize.Fill{Type: "pattern", Pattern: 1, Color: []string{"2F5597"}},
        Alignment: &excelize.Alignment{Horizontal: "center", Vertical: "center"},
    })
    if err != nil {
        return err
    }

    sw, err := f.NewStreamWriter(sheet)
    if err != nil {
        return err
    }
    // 2) 列宽必须在 SetRow 之前调用
    if err := sw.SetColWidth(1, 5, 18); err != nil {
        return err
    }

    headers := make([]interface{}, 5)
    for i, h := range []string{"订单号", "客户", "数量", "单价", "金额"} {
        headers[i] = excelize.Cell{StyleID: headerStyle, Value: h}
    }
    if err := sw.SetRow("A1", headers, excelize.RowOpts{Height: 22}); err != nil {
        return err
    }

    // 3) 逐行追加，行号严格递增
    for i := 2; i <= total+1; i++ {
        qty := rand.Intn(100) + 1
        price := float64(rand.Intn(10000)) / 100
        row := []interface{}{
            fmt.Sprintf("SO-%08d", i-1),
            "客户-" + strconv.Itoa(i%500),
            qty, price, float64(qty) * price,
        }
        if err := sw.SetRow(fmt.Sprintf("A%d", i), row); err != nil {
            return err
        }
    }

    // 4) 收尾：Flush 必须调用，否则最后一批数据不会落盘
    if err := sw.Flush(); err != nil {
        return err
    }
    return f.SaveAs(path)
}
```

这段代码 10 万行 × 5 列的峰值内存通常在 60~120MB 区间（大部分是临时文件缓冲），而不是普通 API 的 2GB+。

### 4.2 按模板填充：导入导出闭环

真实业务很少从零画表，通常是"设计好的模板 + 占位符"。小数据用普通 API（`NewFile` + `SetSheetRow` / `SetCellValue` + `SaveAs`），要直接写 HTTP 响应可用 `WriteTo(io.Writer)` 省一次落盘；模板填充则走 `OpenFile` → 覆盖占位单元格 → 插图 → `WriteToBuffer`。

```go
func fillInvoice(tplPath string, inv Invoice) ([]byte, error) {
    f, err := excelize.OpenFile(tplPath)
    if err != nil {
        return nil, err
    }
    defer f.Close()

    const sheet = "发票"
    for cell, v := range map[string]interface{}{
        "C3": inv.No, "C4": inv.Customer,
        "C5": inv.Date.Format("2006-01-02"), "E8": inv.Total,
    } {
        if err := f.SetCellValue(sheet, cell, v); err != nil {
            return nil, err
        }
    }

    // 明细从固定起始行追加，行高随内容走
    for i, item := range inv.Items {
        r := 10 + i
        _ = f.SetCellValue(sheet, fmt.Sprintf("B%d", r), item.Name)
        _ = f.SetCellValue(sheet, fmt.Sprintf("D%d", r), item.Qty)
        _ = f.SetCellValue(sheet, fmt.Sprintf("E%d", r), item.Price)
        _ = f.SetCellFormula(sheet, fmt.Sprintf("F%d", r), fmt.Sprintf("=D%d*E%d", r, r))
        _ = f.SetRowHeight(sheet, r, 24)
    }

    // 图片锚点：以单元格左上角为基准，偏移量单位为像素
    if inv.Logo != "" {
        if err := f.AddPicture(sheet, "A1", inv.Logo, &excelize.GraphicOptions{
            ScaleX: 0.4, ScaleY: 0.4, OffsetX: 12, OffsetY: 8,
        }); err != nil {
            return nil, err
        }
    }
    buf, err := f.WriteToBuffer()
    if err != nil {
        return nil, err
    }
    return buf.Bytes(), nil
}
```

模板设计约定：占位符放在独立单元格（不要和文字混排），明细区预留 1 行样式供复制，`AddPicture` 只接受服务端可读的本地文件。

### 4.3 样式、合并与维度

```go
// 边框 + 居中 + 自动换行，一次建好反复用
border := []excelize.Border{
    {Type: "left", Color: "BFBFBF", Style: 1}, {Type: "right", Color: "BFBFBF", Style: 1},
    {Type: "top", Color: "BFBFBF", Style: 1}, {Type: "bottom", Color: "BFBFBF", Style: 1},
}
id, _ := f.NewStyle(&excelize.Style{
    Border:    border,
    Alignment: &excelize.Alignment{Horizontal: "center", Vertical: "center", WrapText: true},
})
f.SetCellStyle("Sheet1", "A1", "F1", id) // 区间批量设置，比逐格快得多

f.MergeCell("Sheet1", "A1", "F1")     // 只保留左上角的值
f.SetColWidth("Sheet1", "A", "A", 12) // 单位是字符宽度
f.SetRowHeight("Sheet1", 1, 28)       // 单位是磅
f.SetPanes("Sheet1", &excelize.Panes{Freeze: true, YSplit: 2, TopLeftCell: "A3", ActivePane: "bottomLeft"})
```

v2.9 起 `MergeCell` 会清空被合并区域除左上角外的单元格值——依赖旧行为（合并后仍能读到从属单元格）的代码升级时会踩到。

### 4.4 公式与计算

`SetCellFormula` 只写公式文本，**不会算结果**；Excel 打开时才计算。要在服务端拿值，用 `CalcCellValue`：

```go
f.SetCellFormula("Sheet1", "F2", "=SUM(D2:E2)")
f.SetCellFormula("Sheet1", "F3", "=IFERROR(VLOOKUP(A3,数据!$A:$C,3,FALSE),\"\")")

val, err := f.CalcCellValue("Sheet1", "F2") // 返回格式化后的字符串
if err != nil {
    // 常见：循环引用、引用了外部工作簿、函数未实现
    log.Printf("calc F2: %v", err)
}
```

能力边界必须清楚：

- 支持 400+ 内置函数，但**不支持宏表函数、外部链接引用、部分新版动态数组函数**；
- 结果为 `#N/A` 等错误值时返回错误字符串而非 `error`，需要自己判前缀；
- 计算是**按需递归**的，整表重算没有批量 API，循环里调 `CalcCellValue` 会重复解析依赖树——大批量请自己用 Go 算，再 `SetCellValue` 写死。

### 4.5 图表、校验与条件格式

```go
// 图表：系列引用用绝对引用，放在数据右侧
chart := &excelize.Chart{
    Type: excelize.Col,
    Series: []excelize.ChartSeries{{
        Name:       "订单明细!$E$2",
        Categories: "订单明细!$A$3:$A$1002",
        Values:     "订单明细!$E$3:$E$1002",
    }},
    Title: excelize.ChartTitle{Paragraph: []excelize.RichTextRun{{Text: "金额分布"}}},
    Width: 720, Height: 320,
}
f.AddChart("订单明细", "H2", chart)

// 数据验证：下拉框
f.AddDataValidation("Sheet1", &excelize.DataValidation{
    Sqref: "C2:C500", Type: "list",
    Formula1: `"待付款,已付款,已发货,已完成"`,
    Error:    "请从下拉列表中选择状态",
})

// 条件格式：大于 10000 标红（v2.9 起 Format 是指针）
f.AddConditionalFormat("Sheet1", &excelize.ConditionalFormatOptions{
    Type: "cell", Criteria: "greater than", Value: "10000",
    Format: &excelize.ConditionalStyle{Font: &excelize.Font{Color: "9C0006", Bold: true}},
})
```

注意 `AddChart` 的 `Series` 引用的是**最终工作表名**，之后再 `SetSheetName` 改名会让图表断链；系列区间长度也要与数据实际行数一致。

### 4.6 读大文件与按需转换

```go
func scan(path string) error {
    f, err := excelize.OpenFile(path)
    if err != nil {
        return err
    }
    defer f.Close()

    rows, err := f.Rows("Sheet1")
    if err != nil {
        return err
    }
    defer rows.Close()

    for rows.Next() {
        cols, err := rows.Columns()
        if err != nil {
            return err
        }
        // 日期列：先拿序列号，再转 time.Time
        if len(cols) >= 5 {
            if serial, err := strconv.ParseFloat(cols[4], 64); err == nil {
                if t, err := excelize.ExcelDateToTime(serial, false); err == nil {
                    _ = t.Format(time.RFC3339)
                }
            }
        }
    }
    return rows.Error()
}
```

两个细节：`ExcelDateToTime` 的第二个参数是"是否 1904 基准"（Mac 旧版 Excel 用），传错会整体偏移 4 年；`rows.Error()` 才是迭代过程中的真实错误，`Next()` 返回 false 不代表成功结束。

---

## 5. 版本演进

| 版本 | 时间 | 关键变化 |
|---|---|---|
| v1.x | 2016–2019 | 单包时代，API 无版本后缀，`import "github.com/xuri/excelize"` |
| v2.0 | 2019-05 | 模块路径改为 `/v2`，破坏性重构，引入 `Options` |
| v2.4 | 2021-04 | StreamWriter 成熟，支持 `AddTable` / `MergeCell` / `SetPanes` |
| v2.6 | 2022-04 | 公式引擎大扩，`CalcCellValue` 支持函数数破 300 |
| v2.9.0 | 2024-10 | 新增 `MoveSheet`、`DeleteSlicer`/`GetSlicers`；`MergeCell` 清空从属单元格；`AddDataValidation` 等支持并发 |
| v2.9.1 | 2025-05 | 要求 Go 1.23+；`SetColStyle` 让流式写入支持列样式；新增 `SetCalcProps`/`GetCalcProps`；`SetCellInt` 参数改 `int64`；`Rows` 提速约 20% |
| v2.10.x | 2025-10 / 2026-02 | `Options.TmpDir` 可指定临时目录；`SetZipWriter` 自定义 ZIP 写入器；`GetSheetDimension` 提速约 95% |
| v2.11.x | 2026-07 / 2026-11 | 持续补齐图表类型与数字格式本地化 |

截至 2026-11，上游已发布到 v2.11.x，但 v2.9.x 仍是企业项目中使用最广的一条线。本文所有 API 均以 v2.9 为基线，v2.10/v2.11 保持向后兼容（除 `Format` 指针化、`SetCellInt` 类型这类已标注的破坏性变更）。升级时重点检查 `go.mod` 的 Go 版本、`ConditionalFormatOptions.Format` 是否仍按结构体赋值、`SetCellInt` 传参是否编译失败。

---

## 6. 常见坑与最佳实践

1. **`GetRows` 一把梭会打爆内存**：它返回 `[][]string`，10 万行 × 20 列轻松吃掉 1GB 以上。大文件一律用 `Rows` + `rows.Next()`，并 `defer rows.Close()`。
2. **StreamWriter 之后再调普通 API**：`NewStreamWriter` 之后对同一 sheet 调 `SetCellValue`/`SetCellStyle`，会得到"流已关闭"类错误或损坏文件。要么全流式，要么全普通，**不要混**。
3. **循环里 `NewStyle` 导致文件膨胀**：样式是追加式索引，10 万个样式会让 `styles.xml` 涨到几十 MB。样式必须在循环外创建，循环内只传 ID。
4. **日期是浮点数，不是时间**：Excel 存的是 1900 基准的序列号。写入用 `excelize.TimeToExcelTime`，读取用 `ExcelDateToTime(serial, use1904)`；`use1904` 传错（或模板来自 Mac 旧版）会整体偏移 4 年。另注意 1900 年 2 月 29 日这个历史 bug 的存在。
5. **长数字被当字符串 / 前导零丢失**：18 位订单号用 `SetCellValue` 会按 `float64` 处理，末位变 0。必须用 `SetCellStr` 并配 `NumFmt: "@"`（文本格式）；手机号、身份证同理。
6. **数字被当文本，Excel 左上角绿三角**：反过来的坑。用 `SetCellInt`/`SetCellFloat` 而不是 `SetCellValue(..., "123")`，并确认该列 `NumFmt` 不是文本。
7. **并发写同一个 `File`**：只有少数函数（v2.9 起 `AddDataValidation`、`DeleteDataValidation`、`GetCellStyle`）标注并发安全，其余都会竞争内部 XML 缓冲。正确做法是一个 goroutine 一个 `File`，或按 sheet 拆文件。
8. **忘记 `Flush` 或 `Close`**：StreamWriter 不 `Flush` 最后一批数据不会落盘；`File` 不 `Close` 在 Windows 上会锁住临时文件，导致后续 `os.Remove` 失败（尤其在 `defer` 里忘了）。
9. **`SetCellFormula` 不等于算出值**：`GetCellValue` 读公式单元格返回的是**缓存值**，新建文件里通常为空。需要数值就 `CalcCellValue`，或直接写入计算结果。
10. **图片路径必须是服务端可读的本地文件**：`AddPicture` 不接受 URL，也不接受 `io.Reader`。远程图片要先下载到临时目录，用完清理；`GraphicOptions` 的 `OffsetX/OffsetY` 单位是像素，`ScaleX/ScaleY` 相对原图。
11. **工作表名限制**：最长 31 字符，不能含 `: \ / ? * [ ]`，且不能重名。用 `f.NewSheet(userInput)` 前必须校验，否则返回错误；v2.9 起 `SetSheetName` 区分大小写。
12. **临时目录不可写**：StreamWriter 超过 16MB 会溢写到 `os.TempDir()`。容器里 `/tmp` 只读或磁盘满时会报错，v2.10 起可用 `excelize.Options{TmpDir: "/data/tmp"}` 指定。

---

## 7. 选型对比

| 方案 | 优势 | 短板 | 适用边界 |
|---|---|---|---|
| **excelize** | 功能最全、流式写入、公式引擎、图表/图片/透视表 | API 面大、样式与坐标需手算、非并发安全 | 服务端生成复杂报表、模板填充、需要真 `.xlsx` |
| **tealeg/xlsx** | API 简单、上手快、老项目多 | 维护停滞、样式/图表能力弱、无流式写入、大文件内存差 | 只需读写简单表格、存量代码迁移成本敏感 |
| **encoding/csv** | 零依赖、极快、内存可控 | 无格式/多 sheet/公式，Excel 打开中文易乱码（需 BOM） | 数据交换、批量导入的中间格式、日志导出 |
| **模板渲染（html/template 等）** | 前端思维、布局灵活 | 产出是 HTML，Excel 打开兼容性差、公式与图表不可用 | 邮件内嵌表格、需要"看起来像报表"的轻量场景 |
| **前端 SheetJS（xlsx.js）** | 无需服务端、可离线、能解析用户本地文件 | 大文件阻塞主线程、样式保真度有限、Node 端内存同样吃紧 | 浏览器内预览/小文件导出、纯前端工具 |

选型判断可以压缩成三个问题：

- **要不要"像 Excel"**？要公式、图表、条件格式、多 sheet，只能上 excelize，tealeg/xlsx 和 CSV 都撑不住。
- **数据量多大**？超过 5 万行必须考虑流式；CSV 天然流式但牺牲格式。
- **生成在哪一侧**？服务端生成便于审计、复用模板、控制权限；前端 SheetJS 适合即时导出，但别把百万行丢给浏览器。

常见组合是**服务端 excelize 出正式报表 + CSV 做数据交换 + 前端 SheetJS 做上传预览**，边界按"是否需要格式保真"划。
