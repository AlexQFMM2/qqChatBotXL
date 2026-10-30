# 单词解读预检样本

2026-10-30：以下文字来自最终规则下的真实主模型；平台发送为模拟，未发送 QQ / 微信消息。音频来自同一现有夏莉语音配置的实际合成，WAV 格式与非静音检查通过，尚待真人试听确认发音。

## unhappy

```text
unhappy

音标
英 /ʌnˈhæpi/ ｜ 美 /ʌnˈhæpi/

翻译
不快乐的，不高兴的；不满意的；（事物）不合适的、令人遗憾的，如 an unhappy choice

词性
adj. 形容词

构词拆解
由否定前缀 un-（不）加基础词 happy（快乐的、高兴的）构成，整体表示“不高兴的、不满意的”。

例句
She looked unhappy when she heard the news.
听到那个消息时，她显得很不高兴。
```

[试听 unhappy](../../observations/word-learning-20261030/unhappy.wav)

## reusable

```text
reusable

音标
英 /ˌriːˈjuːzəbl/ ｜ 美 /ˌriːˈjuːzəbl/

翻译
可重复使用的；可再利用的

词性
adj. 形容词

构词拆解
由前缀 re-（再、重新）+ 基础词 use（使用）+ 后缀 -able（能……的）构成，字面即“能被再次使用的”。

例句
Bring a reusable bottle instead of buying bottled water.
带上可重复使用的水瓶，别买瓶装水。
```

[试听 reusable](../../observations/word-learning-20261030/reusable.wav)

## apple

```text
apple

音标
英 /ˈæp.əl/ ｜ 美 /ˈæp.əl/

翻译
苹果；苹果树的果实

词性
n. 名词

构词拆解
无明显可拆分结构，可作为整体词记忆。

例句
She ate an apple for breakfast.
她早餐吃了一个苹果。
```

[试听 apple](../../observations/word-learning-20261030/apple.wav)

## record

```text
record

音标
英 n. /ˈrekɔːd/；v. /rɪˈkɔːd/ ｜ 美 n. /ˈrekərd/；v. /rɪˈkɔːrd/

翻译
1. 记录，档案；唱片；纪录
2. 记录，记载；录制（声音、影像）

词性
1. n. 名词
2. v. 动词

构词拆解
前缀 re-（回、再次）+ 词根 cord-（心，源自拉丁语 cor, cordis）——原义是“反复记在心里”，因此引申为记录。名词和动词同形，但重音位置不同。

例句
Could you record the meeting so we can listen to it later?
你能把会议录下来吗，方便我们之后回听。
```

[试听 record](../../observations/word-learning-20261030/record.wav)

## read

```text
read

音标
英 /riːd/（现在式）；过去式与过去分词 read 读 /red/ ｜ 美 /riːd/（现在式）；过去式与过去分词 read 读 /red/

翻译
1. 阅读，读；朗读；读懂，理解；写着，标示（如标牌上）
2. 读物，阅读（口语，如 a good read 一本好看的书）

词性
1. v. 动词
2. n. 名词

构词拆解
无明显可拆分结构，可作为整体词记忆。注意它是不规则动词：拼写不变，但过去式和过去分词读作 /red/，与现在式 /riːd/ 不同。

例句
She likes to read for a while before going to bed.
她喜欢睡前读一会儿书。
```

[试听 read](../../observations/word-learning-20261030/read.wav)

## 证据边界

文本已核对，但模型生成仍可能出现事实错误，不能把样本通过当作词典级准确性保证。初轮曾出现 record 美音动词音标错误，经收紧规则后最终样本正确，原始结果保留在本机 observations 中。

音频和 JSON 位于被忽略的 observations/，不随 Git 上传；本页只保存合成样本文字与本机音频链接。发布前仍需真人听音和用户手机验收。

## 试用反馈修订样本（2026-10-30）

真实模型、合成身份、模拟平台及语音；来源 `observations/word-learning-pace-release-20261030/reusable.json`。引入放在固定解析前；“只要解析，不要开场白”用例也已验证直接从单词开始。

```text
老师，这个词拆开看就很清楚，环保话题里也常能见到它。

reusable

音标
英 /ˌriːˈjuːzəbl/ ｜ 美 /ˌriːˈjuːzəbl/

翻译
可重复使用的；可再次使用的

词性
adj. 形容词

构词拆解
前缀 re-（再、重新）+ 基础词 use（使用）+ 后缀 -able（可……的）→ 可以再次使用的

例句
Bring a reusable water bottle to reduce plastic waste.
带上可重复使用的水瓶，减少塑料垃圾。
```

学习节奏处理对比在 `observations/word-learning-pace-20261030/`；实际旧 WAV 经同一处理后为 1.32–1.62 秒，保持音调并加入前后停顿。未以时长检查代替人工发音试听。
