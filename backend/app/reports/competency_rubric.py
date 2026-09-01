# ruff: noqa: E501
from __future__ import annotations

from app.reports.scoring_domain import (
    CoreDimension,
    CoreRubric,
    Indicator,
    ModuleRubric,
    SpecialModule,
    Target,
)
from app.sessions.models import Media


def _indicator(target: str, code: str, name: str, observation: str) -> Indicator:
    return Indicator(id=f"{target}.{code}", name=name, observation=observation)


_CORE_RUBRICS: dict[CoreDimension, CoreRubric] = {
    CoreDimension.respectful_communication: CoreRubric(
        id=CoreDimension.respectful_communication,
        name="尊重性沟通与关系维护",
        measures="评价受测者能否在短时间、匿名、缺乏视觉信息的通话中，以尊重、平等、非评判的方式与来电者建立并维持可以继续工作的联系。",
        indicators=[
            _indicator(
                "C1",
                "respect",
                "尊重与非评判",
                "不因来电者的经历、情绪、想法或选择而责备、羞辱、讽刺或进行道德评价",
            ),
            _indicator(
                "C1",
                "equal_stance",
                "平等互动姿态",
                "不把自己置于审问者、教师或裁决者位置，避免盘问、说教和单方面纠正",
            ),
            _indicator(
                "C1",
                "autonomy",
                "表达自主性",
                "尊重来电者暂不回答、需要停顿或对理解作出纠正的权利，不以关系压力迫使披露",
            ),
            _indicator(
                "C1",
                "rupture_detection",
                "关系紧张识别",
                "能发现明显的不满、警惕、退缩、误解或顺从性应付，而不只是继续既定流程",
            ),
            _indicator(
                "C1",
                "repair",
                "关系修复",
                "在回应不合适或出现误解后，能够承认具体问题、调整方式并重新建立合作",
            ),
        ],
        excluded=[
            "情绪判断是否准确，归入C2。",
            "来电焦点是否清楚，归入C3。",
            "是否说明保密、职责和关系边界，归入C7。",
            "来电者最终是否信任或喜欢受测者。",
        ],
        evidence_note="受测者原话、回应顺序、来电者明确提出的纠正或拒绝、受测者后续是否调整。来电者内部的“信任值”不得作为评分证据。",
        anchors={
            0: "出现明显羞辱、指责、歧视、威胁、强迫披露或利用关系施压的行为。",
            1: "能维持表面交流，但经常使用盘问、教育、纠正或单方面说服；对来电者的拒绝和不满缺少回应。",
            2: "基本尊重，没有明显评判；一般交流可以继续，但互动较程序化，遇到警惕、误解或关系波动时调整有限。",
            3: "持续保持尊重、平等和非评判；能够给予表达选择，并处理一般性的犹豫、拒绝和关系紧张。",
            4: "面对明显敌意、羞耻、退缩、反复试探或关系破裂风险时，仍能保持稳定姿态；既不压迫也不过度迎合，并完成有效修复。",
        },
        conditional_in_level3=["处理一般性的犹豫、拒绝和关系紧张"],
    ),
    CoreDimension.listening_and_emotion: CoreRubric(
        id=CoreDimension.listening_and_emotion,
        name="倾听与情绪理解",
        measures="评价受测者能否理解来电者叙述中的事实、情绪及其处境意义，并以准确、试探和便于核对的方式表达这种理解。",
        indicators=[
            _indicator(
                "C2",
                "content_tracking",
                "内容跟随",
                "能跟随来电者正在表达的事情，不随意跳过、误置时间线或抓住次要细节代替主要内容",
            ),
            _indicator(
                "C2",
                "emotion_recognition",
                "情绪识别",
                "能识别明确或含蓄表达的主要情绪，不只复述事件，也不把所有痛苦笼统称为“焦虑”或“难受”",
            ),
            _indicator(
                "C2",
                "situated_understanding",
                "处境性理解",
                "能把情绪放回具体事件、关系、损失、威胁或两难中理解，避免脱离语境贴标签",
            ),
            _indicator(
                "C2",
                "verification",
                "核对与修正",
                "使用试探性表达确认理解；来电者纠正时能够调整，而不是坚持自己的解释",
            ),
            _indicator(
                "C2",
                "ambivalence",
                "矛盾体验容纳",
                "能同时看见来电者并存的愿望、顾虑和情绪，不急于把复杂体验简化成单一原因",
            ),
        ],
        excluded=[
            "是否确定本次通话的工作目标，归入C3。",
            "是否形成整体专业判断，归入C4。",
            "是否采取稳定、建议或问题解决措施，归入C5。",
            "来电者是否因回应而立即平静。",
        ],
        evidence_note="受测者的复述、情绪反映、澄清和核对；来电者对理解作出的明确确认、补充或纠正只能作为辅助证据。",
        anchors={
            0: "否定、贬低或嘲讽来电者的感受；坚持明显错误的解释；用未经证据支持的标签代替倾听。",
            1: "主要跟随事情表面经过，回应以泛泛安慰、重复原话或快速解释为主，持续遗漏重要情绪。",
            2: "能识别主要事件和一种核心情绪，部分回应贴合处境；对含蓄、矛盾或变化中的体验理解不稳定。",
            3: "能准确连接事件、情绪和处境意义，使用试探性语言核对，并依据来电者的纠正及时修正理解。",
            4: "面对多重情绪、含糊表达、羞耻掩饰或叙述失序时，仍能把握体验的变化和矛盾，保持准确而不过度解释。",
        },
        conditional_in_level3=["依据来电者的纠正及时修正理解"],
    ),
    CoreDimension.concern_clarification: CoreRubric(
        id=CoreDimension.concern_clarification,
        name="来电诉求澄清与工作焦点",
        measures="评价受测者能否弄清来电者为什么在此时打来，区分事情经过、当前困扰和实际求助，并与来电者确定本次热线最需要处理的重点。",
        indicators=[
            _indicator(
                "C3", "call_reason", "来电缘由澄清", "了解促使来电者此时求助的直接原因和近期变化"
            ),
            _indicator(
                "C3",
                "current_need",
                "当前需要辨别",
                "区分来电者是在倾诉、寻求信息、需要情绪稳定、希望解决问题，还是面临安全困难",
            ),
            _indicator(
                "C3",
                "prioritization",
                "重点与优先次序",
                "当多个问题同时出现时，能够辨别最紧迫、影响最大或本次最可处理的部分",
            ),
            _indicator(
                "C3",
                "shared_focus",
                "共同确认焦点",
                "通过总结或核对与来电者确认工作重点，而不是由受测者单方面规定话题",
            ),
            _indicator(
                "C3",
                "focus_adjustment",
                "焦点调整",
                "新的重要信息出现后能够重新排序，不因先前设定的计划忽略当前变化",
            ),
        ],
        excluded=[
            "对问题成因和机制的理解，归入C4。",
            "针对焦点采取何种支持，归入C5。",
            "通话时间和话轮节奏，归入C6。",
        ],
        evidence_note="开场探索、阶段性概括、优先事项讨论、来电者对目标的确认或修正，以及后续通话是否围绕已确认重点展开。",
        anchors={
            0: "明显曲解来电目的，强行处理与来电者需要无关的问题；来电者纠正后仍不调整。",
            1: "收集了若干事实，但缺少来电缘由和工作重点；问题随叙述漂移或表现为机械问询。",
            2: "能识别表面来电原因和一个主要问题，但对实际求助、优先次序或共同确认处理不足。",
            3: "能区分事件、困扰和求助需要，与来电者确定适合本次热线处理的重点，并在必要时重新聚焦。",
            4: "面对多个竞争性问题、含糊诉求或不断变化的信息，能够协商出清楚、现实且符合热线定位的优先工作焦点。",
        },
        conditional_in_level3=[],
    ),
    CoreDimension.integration_and_judgment: CoreRubric(
        id=CoreDimension.integration_and_judgment,
        name="信息整合与专业判断",
        measures="评价受测者能否根据已获得的事件、情绪、认知、行为、功能、应对和支持信息，形成有证据、保留不确定性并能够随新信息修正的工作理解。",
        indicators=[
            _indicator(
                "C4",
                "relevance",
                "信息相关性",
                "围绕当前问题了解必要信息，避免漫无目的收集背景或遗漏明显影响判断的方面",
            ),
            _indicator(
                "C4",
                "integration",
                "信息关联",
                "能把诱发事件、主观体验、行为反应、生活功能、既有应对和现实支持联系起来",
            ),
            _indicator(
                "C4",
                "evidence_boundary",
                "证据边界",
                "区分来电者原话、可确认事实、专业推断和仍未知的信息",
            ),
            _indicator(
                "C4",
                "judgment",
                "判断与优先次序",
                "能说明当前最值得关注的问题及理由，判断强度与已有证据相称",
            ),
            _indicator(
                "C4",
                "hypothesis_revision",
                "假设修正",
                "遇到矛盾、纠正或新事实后能够调整原有判断，不执着于单一解释",
            ),
        ],
        excluded=[
            "只是把本次通话主题说清，归入C3。",
            "支持措施是否合适，归入C5。",
            "工作记录文字是否规范，归入C9。",
        ],
        evidence_note="对话中的探索和总结、受测者在工作记录中的问题理解与判断依据、案例实际披露事实。未向受测者披露的案例事实不能用来判定其漏评。",
        anchors={
            0: "作出明显缺乏依据的诊断或结论；编造事实；遗漏已经明确出现的紧迫问题并据此采取不当行动。",
            1: "信息零散且缺少关联，主要重复单项事实；判断依赖直觉、标签或单一原因，未说明不确定性。",
            2: "能概括主要事件、情绪和部分功能影响，形成基本可理解的判断；信息整合、证据边界或修正能力仍不完整。",
            3: "能整合事件、体验、功能、应对和支持信息，清楚区分事实与推断，并根据新信息调整工作理解和优先事项。",
            4: "面对信息不完整、相互矛盾或存在多种解释时，能够保留合理假设、比较证据并形成清楚、可修正且适合热线职责的判断。",
        },
        conditional_in_level3=["根据新信息调整工作理解和优先事项"],
    ),
    CoreDimension.supportive_intervention: CoreRubric(
        id=CoreDimension.supportive_intervention,
        name="支持性介入与协同行动",
        measures="评价受测者能否把前面的理解转化为与来电者当前状态、需求和现实条件相匹配的支持，并与来电者共同形成能够执行的下一步。",
        indicators=[
            _indicator(
                "C5",
                "fit",
                "介入匹配",
                "支持方式与已经确认的问题、情绪强度、认知状态和热线阶段相符",
            ),
            _indicator(
                "C5",
                "resources",
                "既有应对与资源利用",
                "了解并使用来电者已有的有效经验、个人能力和现实支持，不把来电者视为完全被动",
            ),
            _indicator(
                "C5",
                "timing",
                "建议时机与方式",
                "在充分理解后提供建议、信息或方法，避免过早、过量和脱离处境的方案输出",
            ),
            _indicator(
                "C5",
                "shared_choice",
                "共同选择",
                "提供有限、清楚的选择，讨论顾虑、偏好和可行性，不替来电者做非必要决定",
            ),
            _indicator(
                "C5",
                "feedback_adjustment",
                "反馈与调整",
                "询问支持是否合适；遭到拒绝、无效或实施困难时能够修正，而不是重复原建议",
            ),
            _indicator(
                "C5",
                "action_layers",
                "行动层次",
                "区分眼下需要完成的事情和通话后的后续安排，行动具体且责任主体清楚",
            ),
        ],
        excluded=[
            "来电者是否最终接受或执行建议。",
            "服务是否越过伦理和职责范围，归入C7。",
            "高风险状态下的安全行动，另由S2评价。",
        ],
        evidence_note="受测者提出支持的时机、内容和依据，双方对可行性的讨论，来电者明确提出的困难，以及受测者是否调整。",
        anchors={
            0: "提供明显危险、羞辱性或强迫性的建议；以个人价值观代替专业支持；推动与已知处境明显冲突的行动。",
            1: "主要依靠泛泛安慰、说教或成串建议；支持与前面的理解联系较弱，也未确认可行性。",
            2: "能提供至少一种基本适当的支持或行动，但对既有资源、选择、执行困难或反馈调整考虑不足。",
            3: "支持与当前需要相匹配，能够利用来电者已有能力和资源，共同形成具体可行的行动，并依据反馈进行调整。",
            4: "面对多重限制、拒绝、失败经验或意见冲突时，仍能保持合作，灵活组合支持方式并形成分层、现实的行动安排。",
        },
        conditional_in_level3=["依据反馈进行调整"],
    ),
    CoreDimension.voice_and_process: CoreRubric(
        id=CoreDimension.voice_and_process,
        name="语音沟通与通话进程管理",
        measures="评价受测者能否在缺乏视觉信息的条件下，以清楚、可跟随的语言和适当节奏组织通话，并处理语音交流中的停顿、重叠、听不清和阶段转换。",
        indicators=[
            _indicator(
                "C6",
                "clarity",
                "口语可理解性",
                "表达简明、指向清楚，避免过多术语、长篇解释和含糊指令",
            ),
            _indicator(
                "C6",
                "turn_space",
                "话轮与回应空间",
                "能让来电者完成表达，控制一次提问或说明的负荷，避免持续抢话和连续堆叠问题",
            ),
            _indicator(
                "C6",
                "cue_adaptation",
                "交流线索适配",
                "能根据可靠识别的交流线索调整自己的语速和表达密度，包括来电者的停顿长度、迟疑、打断与重叠，以及其言语内容中表达的状态",
            ),
            _indicator(
                "C6",
                "interruption_handling",
                "沉默与交流中断处理",
                "能区分普通停顿、思考、情绪停顿和技术中断，并以适当方式确认和恢复交流",
            ),
            _indicator(
                "C6",
                "structure",
                "通话结构",
                "开场、探索、工作和收束阶段基本清楚；能够使用简短总结和过渡维持方向",
            ),
            _indicator(
                "C6",
                "time_use",
                "时间使用",
                "在有限通话时间内保持重点，既不仓促推进，也不让通话长期停留在重复内容中",
            ),
        ],
        excluded=[
            "声音是否悦耳、是否符合某种性别或播音审美。",
            "单一停顿时长、语速数值或打断次数本身。",
            "强烈情绪失控和长时无法交流的专项处理，归入S3。",
            "最终结束质量，归入C8。",
        ],
        evidence_note="只使用能够从原始音频中可靠识别的事件，例如重叠区间、静音时长和受测者自身语速变化。合成语音的韵律参数不作为来电者情绪线索的证据。其他证据包括说话人时间轴、对话内容和技术故障记录；ASR分段不得直接代替话轮判断。",
        anchors={
            0: "持续使用明显压迫、嘲讽或失控的声音表达，或者在已知听不清和无法跟随时仍强行推进，导致通话无法正常进行。",
            1: "表达冗长或含糊，频繁抢话、堆叠问题或忽略交流中断；通话缺少基本结构，来电者难以参与。",
            2: "语言基本清楚，能够完成普通话轮交换；偶有节奏、负荷或阶段组织问题，但未持续破坏通话。",
            3: "能根据可靠识别的交流线索调整节奏，合理处理停顿、重叠和听不清，并以清楚的过渡和总结维持通话方向。",
            4: "面对表达反复、交流线索复杂、偶发技术中断或多次阶段变化时，仍能迅速判断并恢复自然、稳定、有重点的通话过程。",
        },
        conditional_in_level3=["合理处理停顿、重叠和听不清"],
    ),
    CoreDimension.boundary_and_ethics: CoreRubric(
        id=CoreDimension.boundary_and_ethics,
        name="服务边界与伦理责任",
        measures="评价受测者是否了解热线能够提供的服务、保密和安全责任，在自己的岗位及能力范围内工作，并以透明、尊重来电者权利的方式处理边界问题。",
        indicators=[
            _indicator(
                "C7",
                "role_scope",
                "角色与服务范围",
                "对热线能够提供的支持和不能承担的任务保持准确理解，不把一次热线包装成诊断、治疗或长期照护",
            ),
            _indicator(
                "C7",
                "privacy",
                "隐私与保密",
                "在相关情景出现时准确说明保密、必要例外及信息使用目的，不作绝对保密承诺",
            ),
            _indicator(
                "C7",
                "relationship_boundary",
                "专业关系边界",
                "不建立私人、排他性、经济性或其他超出服务关系的联系，不用自我披露转移工作重点",
            ),
            _indicator(
                "C7",
                "informed_participation",
                "知情参与与自主",
                "对重要评估、资源联系和可能的升级处置说明目的，允许来电者表达顾虑并参与决定",
            ),
            _indicator(
                "C7",
                "competence_scope",
                "胜任力与责任范围",
                "识别自身和热线能力限制，需要时提出适当的专业资源、机构流程或进一步服务",
            ),
            _indicator(
                "C7",
                "integrity",
                "信息与行动诚信",
                "不夸大资源效果，不承诺无法完成的事项，不把拟采取行动表述为已经执行",
            ),
        ],
        excluded=[
            "日常互动是否尊重，归入C1。",
            "资源方案是否实际可行，归入C5。",
            "高风险情况下具体升级行动是否充分，归入S2。",
        ],
        evidence_note="受测者对角色、保密、资源和处置目的的说明，边界请求出现后的回应，以及工作记录中对行动和限制的表述。",
        anchors={
            0: "作出绝对保密或无法兑现的承诺；建立私人或排他性关系；严重越权；未经必要判断泄露信息；伪造外部处置。",
            1: "对服务范围、保密或关系边界理解含糊，出现过度承诺、替来电者决定或提供超出热线能力的安排。",
            2: "基本保持在热线职责范围内，能说明部分限制；对知情参与、保密例外或复杂边界的处理不够完整。",
            3: "能在相关时机清楚说明角色、保密、限制和资源用途，尊重来电者自主，并在需要时使用合适的服务路径。",
            4: "面对自主、安全、隐私和机构责任相互冲突的复杂情境，能够作出透明、最小必要、证据充分且可解释的处理。",
        },
        conditional_in_level3=[],
    ),
    CoreDimension.closure_and_followup: CoreRubric(
        id=CoreDimension.closure_and_followup,
        name="通话结束与后续安排",
        measures="评价受测者能否识别结束条件，向来电者清楚传达通话正在收束，回顾已经完成的工作，并确认通话后的行动和继续求助路径。",
        indicators=[
            _indicator(
                "C8",
                "timing",
                "结束时机",
                "综合通话目标、来电者当前状态、未完成事项和时间条件判断是否适合结束",
            ),
            _indicator(
                "C8", "notice", "结束预告", "以自然、明确的方式提示通话即将结束，避免突然中止"
            ),
            _indicator(
                "C8",
                "review",
                "共同回顾",
                "简要总结已经谈清的内容、仍未解决的问题和本次通话的实际进展",
            ),
            _indicator(
                "C8",
                "status_action",
                "状态与行动确认",
                "确认来电者当前是否能够结束通话，以及下一步由谁、何时、做什么",
            ),
            _indicator(
                "C8",
                "continuity",
                "连续性安排",
                "根据需要说明再次来电、其他服务、现实支持或紧急求助路径，不作无法兑现的复联承诺",
            ),
            _indicator(
                "C8",
                "caller_ending",
                "主动结束应对",
                "来电者准备挂断、拒绝继续或突然中止时，能够在现有时间内完成必要回应和记录",
            ),
        ],
        excluded=[
            "通话中段的时间和结构管理，归入C6。",
            "高危来电能否结束以及结束前的安全条件，归入S2。",
            "来电者挂断以后系统是否自动重拨。",
        ],
        evidence_note="结束前后的完整话轮、双方确认的行动、来电者主动结束时受测者已有的处理，以及工作记录中的未完成事项。",
        anchors={
            0: "在已经明确的紧迫问题或重要未完成事项仍存在时突然结束，且没有说明、确认或必要安排。",
            1: "主要因时间或受测者意愿结束；缺少结束预告、回顾、状态确认和后续安排。",
            2: "能提示结束，并完成基本总结或下一步安排中的部分内容；结束仍较程序化或存在明显遗漏。",
            3: "结束时机合理，能够共同回顾、确认当前状态和具体行动，说明适当的继续求助路径并自然结束。",
            4: "面对来电者突然想挂断、对结束不满、行动尚未落实或多项遗留问题时，仍能分清必要事项，完成负责任且不过度拖延的收束。",
        },
        conditional_in_level3=["共同回顾、状态确认和行动确认"],
    ),
    CoreDimension.documentation: CoreRubric(
        id=CoreDimension.documentation,
        name="工作记录的准确性与可追溯性",
        measures="评价受测者能否把通话中的关键材料、专业判断、已完成工作、未完成事项和后续计划写成准确、简洁、可核对的职业记录。",
        indicators=[
            _indicator(
                "C9",
                "fact_accuracy",
                "事实准确",
                "记录与实际对话相符，不遗漏或改写影响判断的关键信息",
            ),
            _indicator(
                "C9",
                "source_distinction",
                "来源区分",
                "区分来电者原话、客观通话事实、受测者判断和尚未核实的信息",
            ),
            _indicator(
                "C9",
                "traceability",
                "判断可追溯",
                "记录中的重要结论标注了对应的通话依据，读者能够据此回到原始材料核对；不以一句笼统概括代替说明",
            ),
            _indicator(
                "C9",
                "action_state",
                "行动状态区分",
                "清楚区分已经讨论、已经同意、已经完成、拟采取和未能完成的行动",
            ),
            _indicator(
                "C9",
                "limitations",
                "缺失与限制",
                "记录没有问到、没有得到回答、受技术或通话中断影响的材料及判断限制",
            ),
            _indicator(
                "C9",
                "professional_language",
                "专业表达",
                "语言中性、具体、克制，避免污名化、文学化、推测性和模板化表述",
            ),
        ],
        excluded=[
            "专业判断本身是否充分，归入C4或相应专项模块。",
            "书写字数和格式美观程度。",
            "工作记录是否与系统预设答案逐字一致。",
        ],
        evidence_note="工作记录全文与原始通话、披露事实、行动事件的逐项对照。",
        anchors={
            0: "未提交有效记录，或者编造关键事实、风险证据和已执行行动；记录存在严重误导性或污名化内容。",
            1: "主要只有结论或模板化概述；存在明显事实错误，事实与推断、已做与拟做相互混淆。",
            2: "基本记录来电问题和主要行动，整体与对话相符；证据依据、未知信息、行动状态或表达准确性仍有明显缺口。",
            3: "记录准确、相关且可追溯，清楚区分事实、判断、未知信息及行动状态，并说明重要限制和后续事项。",
            4: "面对复杂、矛盾或信息不完整的通话，仍能形成结构清楚、证据充分、不过度推断并可支持后续专业工作的记录。",
        },
        conditional_in_level3=[],
    ),
}


_MODULE_RUBRICS: dict[SpecialModule, ModuleRubric] = {
    SpecialModule.basic_risk_screening: ModuleRubric(
        id=SpecialModule.basic_risk_screening,
        name="基础风险筛查",
        measures="所有通话默认启用。依据国家卫生健康委《心理援助热线技术指南（试行）》对每次接听均应评估来电者当下自杀危险的要求，本模块不以来电者是否主动表现风险线索为启用前提。\n评价受测者是否在本次通话中以适当方式触及来电者当下的安全风险。本模块只评价筛查动作本身，不要求完成完整评估流程；完整评估的层次与质量归S1b。\n唯一免评情形是来电者在受测者获得任何筛查机会前主动结束通话，或者技术中断导致材料无效。",
        activation="所有通话默认启用，不以来电者是否主动表现风险线索为前提",
        default_enabled=True,
        indicators=[
            _indicator(
                "S1a",
                "screening_scope",
                "筛查覆盖",
                "在通话中触及死亡、自杀或自伤想法，不因来电者未主动提及而完全跳过",
            ),
            _indicator(
                "S1a",
                "wording",
                "提问方式",
                "使用直接、平静、可回答的措辞，不以委婉暗示、诱导性否定或道德化表达代替提问",
            ),
            _indicator(
                "S1a",
                "timing",
                "时机适当",
                "在建立基本联系之后、通话结束之前完成；不在开场机械插入，也不拖到收束时敷衍带过",
            ),
            _indicator(
                "S1a",
                "followup",
                "回应跟进",
                "对来电者的回答作出回应；出现肯定或含糊回答时进入进一步了解，不停留在一次问答",
            ),
            _indicator(
                "S1a",
                "denial_handling",
                "否认结果处理",
                "来电者否认时如实记录否认本身，不把一次否认等同于本次通话无风险",
            ),
        ],
        excluded=[
            "完整风险评估的层次、覆盖面与研判质量，归S1b。",
            "风险确认后的安全安排与升级处置，归S2。",
            "提问时的关系姿态与非评判性，归C1。",
            "工作记录中风险内容的书写质量，归C9。",
        ],
        evidence_note="受测者提出筛查问题的原话及其所在话轮位置、来电者的回答、受测者的后续跟进，以及工作记录中对风险方向的记载。案例是否声明风险事实不影响本模块启用。",
        anchors={
            0: "以道德、法律或羞耻方式阻止来电者谈及自杀想法；或在来电者主动提及后予以否定、嘲讽或转移话题。",
            1: "整通通话未触及风险方向；或仅以“你不会做傻事吧”这类诱导否定的方式带过，且对来电者的回答未作任何回应。",
            2: "触及了风险方向，但表达委婉含糊、时机明显不当，或对来电者的回答未作跟进。",
            3: "在适当时机以直接、平静的方式询问，对回答作出回应；来电者否认时如实记录，不据此断定本次通话无风险。",
            4: "面对回避、羞耻、含糊或明显话题阻力时，仍能自然完成询问并维持关系，并据回答判断是否需要进入完整研判。",
        },
        conditional_in_level3=[],
    ),
    SpecialModule.full_risk_appraisal: ModuleRubric(
        id=SpecialModule.full_risk_appraisal,
        name="完整风险研判",
        measures="只有案例提供了足以观察风险评估层次和研判质量的材料时启用。基础筛查归S1a；本模块评价受测者能否识别和澄清风险信息，并依据已经获得的证据判断风险的性质、动态变化和紧迫程度。",
        activation="案例提供了足以观察风险评估层次和研判质量的材料",
        indicators=[
            _indicator("S1b", "cue_recognition", "线索识别", "识别直接、间接和情境性风险线索"),
            _indicator(
                "S1b",
                "direct_question",
                "直接询问",
                "直接询问死亡、自杀或自伤想法，不用委婉暗示代替关键问题",
            ),
            _indicator(
                "S1b",
                "urgency",
                "紧迫性信息",
                "根据回答了解意图、计划、方法可及性、时间、既往行为和近期变化",
            ),
            _indicator(
                "S1b",
                "risk_protection",
                "风险与保护因素",
                "了解绝望、冲动、物质使用、独处、现实支持及保护因素",
            ),
            _indicator(
                "S1b",
                "appraisal",
                "综合研判",
                "综合正反证据判断紧迫程度，不把主动来电、否认计划或存在保护因素等同于安全",
            ),
            _indicator("S1b", "limitations", "限制说明", "在专业判断中保留未获得的信息和判断限制"),
        ],
        excluded=[
            "是否完成基础筛查，归S1a。",
            "风险确认后的现实安全行动与升级处置，归S2。",
            "风险提问中的尊重和非评判姿态，归C1。",
            "工作记录是否准确标注证据和限制，归C9；本模块只评价研判本身是否有依据。",
        ],
        evidence_note="来电者实际披露的风险信息、受测者的追问与澄清、前后判断变化，以及工作记录中的风险结论。案例尚未披露的风险事实不得用于判定受测者漏评。",
        anchors={
            0: "已出现明确风险线索却否定、嘲讽、鼓励危险行为，或用道德、法律和羞耻方式阻止披露。",
            1: "对风险线索明显回避，或者只作含糊询问便得出安全结论，信息不足以支持判断。",
            2: "能直接询问风险并覆盖部分紧迫性信息，但计划、可及性、时间、既往行为、支持或动态变化存在重要缺口。",
            3: "能直接、平静并有层次地完成风险评估，综合风险与保护信息，判断与实际披露证据相符。",
            4: "面对否认、矛盾、羞耻、反复改变说法或信息不完整时，仍能保持关系，澄清动态风险并清楚说明判断边界。",
        },
        conditional_in_level3=[],
    ),
    SpecialModule.safety_response: ModuleRubric(
        id=SpecialModule.safety_response,
        name="高危状态下的安全处置与协作",
        measures="只有已披露信息提示需要立即形成安全安排、现实支持或升级处置时启用。评价受测者能否把风险判断转化为眼下可执行、能够核对且符合热线职责的安全行动。",
        activation="已披露的信息提示需要立即形成安全安排或升级处置",
        indicators=[
            _indicator(
                "S2", "connection", "持续连接", "在必要时保持连接，避免来电者在高危状态下独自中断"
            ),
            _indicator(
                "S2",
                "real_world_safety",
                "现实安全条件",
                "确认当前位置、独处情况和能够及时到场的现实支持",
            ),
            _indicator(
                "S2",
                "reduce_access",
                "危险可及性降低",
                "与来电者协商降低危险手段可及性或远离危险环境",
            ),
            _indicator(
                "S2",
                "transparent_collaboration",
                "透明协作",
                "说明收集信息和升级处置的目的，处理来电者对失去控制、保密和求助后果的担忧",
            ),
            _indicator(
                "S2",
                "escalation",
                "升级与交接",
                "根据紧迫性使用热线所属机构规定的督导、紧急服务或其他升级路径",
            ),
            _indicator(
                "S2",
                "verification",
                "行动核实",
                "确认行动是否真正同意、开始或完成，不用空泛“保证不会做”代替安全安排",
            ),
        ],
        excluded=[
            "风险线索识别、信息覆盖和风险等级研判，归S1b。",
            "普通情境中的支持性行动，归C5。",
            "保密、自主和岗位范围的一般伦理判断，归C7；本模块只评价高危状态下如何落实安全行动。",
            "交接、行动和未完成事项在工作记录中的书写质量，归C9。",
        ],
        evidence_note="风险信息已经披露后的完整互动片段、受测者提出的安全行动、来电者对行动的明确回应、系统记录的行动状态和工作记录。工作记录中写“拟采取”不能证明通话中已经执行。",
        anchors={
            0: "已知存在紧迫危险却直接结束通话，鼓励危险行为，或者伪造已经启动的紧急处置。",
            1: "只作一般安慰、劝阻或要求安全承诺，没有形成与紧迫程度相称的现实行动。",
            2: "提出部分安全措施或资源，但对位置、独处、支持到场、可及性、行动确认或升级路径处理不完整。",
            3: "能保持连接并与来电者落实匹配的安全行动，确认现实支持和必要升级，行动状态清楚可核对。",
            4: "面对拒绝、位置隐瞒、支持失联、临时变化或多重阻碍时，能够持续更新判断，协商替代方案并完成稳妥交接。",
        },
        conditional_in_level3=[],
    ),
    SpecialModule.emotional_dysregulation: ModuleRubric(
        id=SpecialModule.emotional_dysregulation,
        name="强烈情绪、长时沉默与表达失序处理",
        measures="来电者出现持续哭泣、明显情绪失控、长时沉默、反复中断或暂时无法形成连贯表达时启用。普通思考停顿不启动本模块。",
        activation="出现持续哭泣、明显失控、长时沉默或暂时无法有效交流",
        indicators=[
            _indicator(
                "S3",
                "state_detection",
                "状态辨别",
                "判断当前是情绪停顿、思考、拒绝、交流能力下降还是技术中断",
            ),
            _indicator(
                "S3",
                "load_reduction",
                "负荷降低",
                "降低语言和问题负荷，使用简短、可回应的表达维持联系",
            ),
            _indicator(
                "S3",
                "silence_tolerance",
                "沉默容纳",
                "允许必要沉默，不因受测者自身焦虑连续填满空间",
            ),
            _indicator(
                "S3",
                "stabilization",
                "稳定支持",
                "根据来电者状态使用适当的稳定方法，并说明正在做什么",
            ),
            _indicator(
                "S3",
                "work_recovery",
                "恢复工作",
                "状态稍有恢复后重新确认当前需要和通话方向，不急于追补全部背景",
            ),
            _indicator(
                "S3",
                "safety_attention",
                "安全留意",
                "判断强烈失控是否伴随需要启用S1b或S2的风险信息",
            ),
        ],
        excluded=[
            "普通思考停顿、一般话轮和常规节奏调整，归C6。",
            "来电者情绪及处境是否被准确理解，归C2。",
            "基础风险筛查、完整风险研判和高危安全处置，分别归S1a、S1b和S2。",
        ],
        evidence_note="出现强烈情绪、长时沉默或表达失序前后的完整音频和对话片段、可靠的静音及重叠事件、受测者的调整过程和技术故障记录。Actor内部情绪值只用于确认机会，不作为处理有效的证据。",
        anchors={
            0: "责备、嘲讽或催促来电者停止哭泣；在明显无法交流时持续逼问或直接抛下来电者。",
            1: "主要依靠“别哭了”“冷静一点”等空泛要求，或者持续讲话填满沉默，未根据交流能力作出调整。",
            2: "能放慢速度并尝试保持联系或稳定，但对状态判断、问题负荷、恢复时机或安全留意处理不完整。",
            3: "能准确判断交流状态，控制语言负荷，容纳沉默并提供适当稳定支持，在恢复后自然重新建立工作方向。",
            4: "面对反复失控、表达断裂、沉默与短暂恢复交替出现时，仍能保持稳定联系，灵活调整并同时留意安全变化。",
        },
        conditional_in_level3=[],
    ),
    SpecialModule.psychotic_experience: ModuleRubric(
        id=SpecialModule.psychotic_experience,
        name="精神病性体验与现实检验困难处理",
        measures="来电者出现幻觉、妄想样体验、明显现实判断困难或严重思维紊乱时启用。评价重点是受测者如何理解这些体验对当下生活和安全的影响，而不是能否作出诊断。",
        activation="出现幻觉、妄想样体验、明显现实判断困难或严重思维紊乱",
        indicators=[
            _indicator(
                "S4",
                "experience_response",
                "体验回应",
                "不嘲讽、不争辩，也不直接确认明显不可靠的信念为事实",
            ),
            _indicator("S4", "impact", "影响评估", "了解体验对恐惧、行为、睡眠、功能和安全的影响"),
            _indicator("S4", "communication", "沟通适配", "使用简单、具体、可跟随的语言维持交流"),
            _indicator(
                "S4",
                "service_judgment",
                "服务判断",
                "区分能够继续热线交流、需要现实支持和需要医疗或紧急服务的情况",
            ),
            _indicator(
                "S4",
                "referral",
                "信息与转介",
                "在提供信息或转介时避免污名化，并确认来电者能够理解和执行",
            ),
        ],
        excluded=[
            "是否作出精神障碍诊断，本测评不评价也不要求。",
            "一般关系姿态和情绪理解，分别归C1和C2。",
            "同时出现的自伤、他伤或即时危险，另行启用S1b和S2。",
            "工作记录中的用语和证据标注，归C9。",
        ],
        evidence_note="来电者实际表达的异常体验及其影响、受测者的回应和探索、资源说明与行动讨论。案例内部诊断或未披露事实不能替代通话证据。",
        anchors={
            0: "嘲讽、激烈争辩、故意强化危险信念，或者在明显安全风险下完全忽略。",
            1: "只围绕体验真假争论，或者仓促贴上诊断标签，未了解生活影响和当前安全。",
            2: "能保持基本尊重并了解部分影响，提出一般性专业求助建议；交流适配和安全判断仍不完整。",
            3: "能在不争辩也不附和的情况下了解体验、功能和安全，使用清楚语言并提出适当支持或服务路径。",
            4: "面对高度确信、思维跳跃、警惕或拒绝就医时，仍能保持联系，围绕可共同确认的现实需要形成可行安排。",
        },
        conditional_in_level3=[],
    ),
    SpecialModule.dependency_and_boundary: ModuleRubric(
        id=SpecialModule.dependency_and_boundary,
        name="反复来电、依赖与边界压力处理",
        measures="来电者反复致电、要求固定咨询员、要求私人联系、以关系威胁换取服务或反复讨论相同问题时启用。",
        activation="来电者反复求助、要求排他性联系或以关系压力突破服务边界",
        indicators=[
            _indicator(
                "S5",
                "pattern",
                "模式识别",
                "识别反复来电背后的情绪、功能和现实需要，不把来电者简单视为“麻烦”",
            ),
            _indicator("S5", "boundary", "边界说明", "保持尊重，同时清楚说明热线服务和关系边界"),
            _indicator(
                "S5", "dependency", "依赖控制", "避免用特殊承诺、私人联系或无限延长通话缓解眼前压力"
            ),
            _indicator(
                "S5",
                "continuity",
                "连续性回顾",
                "回顾既往讨论和已尝试行动，避免每次从头重复或机械驱赶",
            ),
            _indicator(
                "S5",
                "alternatives",
                "替代安排",
                "与来电者形成可持续的热线使用方式、其他支持或长期服务安排",
            ),
            _indicator(
                "S5",
                "relationship_pressure",
                "关系压力处理",
                "面对愤怒、失望和被抛弃感时能够维护关系而不取消必要边界",
            ),
        ],
        excluded=[
            "普通通话中的角色、保密和关系边界，归C7。",
            "一般关系紧张和修复，归C1。",
            "替代支持本身是否适合且可行，归C5；本模块评价其是否用于减少依赖和维持连续性。",
            "工作记录是否准确，归C9。",
        ],
        evidence_note="案例提供的既往来电摘要、本次边界请求、受测者对既往工作的回顾、边界说明和替代安排，以及来电者施加关系压力后的完整互动。",
        anchors={
            0: "羞辱、驱赶或报复来电者；建立私人、排他性联系；利用来电者依赖满足个人需要。",
            1: "只重复规定或直接拒绝，未理解边界请求的功能；或者因害怕冲突作出无法维持的特殊承诺。",
            2: "能说明基本边界并保持礼貌，但对反复求助模式、关系反应和替代支持处理不足。",
            3: "能同时回应关系需要和设定边界，回顾既往工作，并共同形成更可持续的求助和支持安排。",
            4: "面对反复试探、愤怒、威胁挂断或多次边界冲突时，仍能保持一致、尊重和连续性，不强化依赖也不粗暴中断关系。",
        },
        conditional_in_level3=[],
    ),
    SpecialModule.aggression_and_harassment: ModuleRubric(
        id=SpecialModule.aggression_and_harassment,
        name="攻击、骚扰与非服务性来电处理",
        measures="来电者持续辱骂、威胁咨询员、进行性骚扰、恶意占线，或在说明后仍明显偏离热线服务用途时启用。",
        activation="出现持续辱骂、性骚扰、恶意占线或明显偏离热线用途的行为",
        indicators=[
            _indicator(
                "S6",
                "behavior_detection",
                "行为辨别",
                "区分痛苦状态下的激烈表达与持续针对咨询员的攻击或骚扰",
            ),
            _indicator(
                "S6",
                "stable_response",
                "稳定回应",
                "保持冷静、简短和清楚，不以羞辱、争吵或报复回应",
            ),
            _indicator(
                "S6", "minimum_conditions", "最低互动条件", "说明热线可以继续提供服务的最低互动条件"
            ),
            _indicator(
                "S6",
                "adjustment",
                "调整机会",
                "在适当时给予一次清楚的调整机会，并说明继续行为的后果",
            ),
            _indicator(
                "S6",
                "closure_record",
                "结束与记录",
                "根据机构规则结束不适合继续的通话，并记录行为和处理过程",
            ),
        ],
        excluded=[
            "普通的不满、质疑和关系紧张，归C1。",
            "自杀、自伤、他伤和其他安全风险，归S1a、S1b或S2。",
            "一般服务边界和伦理责任，归C7；本模块只评价攻击、骚扰或非服务性行为下的设限。",
            "工作记录的准确性和表达质量，归C9。",
        ],
        evidence_note="攻击、骚扰或非服务性行为发生前后的完整对话、受测者的设限原话、是否提供调整机会、继续或结束通话的条件，以及工作记录中的事件描述。",
        anchors={
            0: "与来电者对骂、羞辱、威胁或进行报复性披露；因受到冒犯而忽略明确的紧急风险。",
            1: "在愤怒中失去稳定，边界含糊或反复争论；突然挂断且没有必要说明。",
            2: "能保持基本冷静并说明限制，但对行为性质、调整机会、结束条件或风险线索处理不完整。",
            3: "能区分痛苦表达和骚扰行为，清楚设限、提供调整机会，并按规则结束或恢复服务。",
            4: "面对攻击、操纵和真实求助线索交织的复杂来电，仍能保持安全、边界和必要支持之间的准确平衡。",
        },
        conditional_in_level3=[],
    ),
    SpecialModule.third_party_call: ModuleRubric(
        id=SpecialModule.third_party_call,
        name="第三方来电处理",
        measures="来电者为家人、朋友、同事或其他人求助，要求评估未在场者、获取其信息或让热线直接采取行动时启用。",
        activation="来电者为他人求助、报告风险或要求热线直接处理未在场者",
        indicators=[
            _indicator(
                "S7",
                "identity_purpose",
                "身份与目的",
                "澄清来电者与当事人的关系、来电目的和目前掌握的信息",
            ),
            _indicator(
                "S7",
                "evidence_boundary",
                "证据边界",
                "不把第三方描述直接当作对未在场者的诊断或完整评估",
            ),
            _indicator("S7", "safety", "安全辨别", "了解是否存在需要立即处理的人身安全问题"),
            _indicator(
                "S7",
                "actionable_focus",
                "可行动焦点",
                "把工作重点放在来电者能够采取的支持、沟通和求助行动上",
            ),
            _indicator(
                "S7",
                "information_boundary",
                "信息边界",
                "准确说明信息、隐私和热线行动范围，不泄露其他服务对象信息",
            ),
            _indicator(
                "S7", "help_path", "求助路径", "必要时说明如何鼓励当事人本人求助或使用现实紧急资源"
            ),
        ],
        excluded=[
            "对未在场者作完整心理评估，本模块明确不支持这类推断。",
            "来电者本人的一般关系和情绪支持，分别归C1、C2和C5。",
            "已确认紧急危险后的安全处置，归S2。",
            "隐私和服务范围的一般伦理质量，归C7；本模块只评价第三方情境中的特殊边界。",
        ],
        evidence_note="第三方关于身份、关系和事件的陈述，受测者对信息来源和限制的澄清，对当前安全的询问，以及围绕来电者可采取行动所形成的讨论。",
        anchors={
            0: "对未在场者作出武断诊断，泄露受保护信息，或者在明确紧急危险下提供明显错误的处理方向。",
            1: "完全接受第三方单一叙述并替其作决定，或只要求“让本人打来”而没有处理当前可做事项。",
            2: "能澄清基本关系和目的，提供一般支持；对证据限制、安全、隐私或行动范围处理不够完整。",
            3: "能区分第三方信息和当事人评估，处理必要安全问题，并帮助来电者形成适当、现实的支持行动。",
            4: "面对关系冲突、信息矛盾、当事人拒绝求助或安全责任不清时，仍能谨慎判断证据边界并形成可执行方案。",
        },
        conditional_in_level3=[],
    ),
    SpecialModule.minor_protection: ModuleRubric(
        id=SpecialModule.minor_protection,
        name="未成年人或疑似受侵害来电处理",
        measures="来电涉及未成年人、疑似虐待、性侵害、家庭暴力或其他需要特殊保护的情形时启用。具体报告和升级责任必须结合测评所模拟机构的工作制度及适用法律配置，不能由通用模型自行推定。",
        activation="涉及未成年人、虐待、性侵害、家庭暴力或其他需要特殊保护的情形",
        indicators=[
            _indicator(
                "S8", "development_fit", "发展适配", "使用与年龄、理解能力和当前情绪相适合的语言"
            ),
            _indicator(
                "S8", "necessary_facts", "必要事实", "在不诱导、不审讯的前提下了解必要的安全事实"
            ),
            _indicator(
                "S8",
                "current_safety",
                "当前安全",
                "识别当下是否仍与可能的伤害者共同生活或处于即时危险",
            ),
            _indicator(
                "S8",
                "confidentiality_protection",
                "保密与保护说明",
                "说明保密边界、必要信息使用和可能的保护行动，避免无法兑现的承诺",
            ),
            _indicator(
                "S8",
                "role_responsibility",
                "角色与责任",
                "区分来电者意愿、监护人角色和机构保护责任",
            ),
            _indicator(
                "S8",
                "protection_resources",
                "保护资源",
                "按模拟机构配置的程序讨论现实支持、医疗、保护或紧急资源",
            ),
        ],
        excluded=[
            "一般的保密、知情参与和服务范围，归C7；本模块评价特殊保护情境中的具体处理。",
            "已确认即时危险后的高危安全行动，归S2。",
            "工作记录是否准确区分事实、判断和行动，归C9。",
            "机构未配置的法律和报告程序不得由评分模型自行补充后用于扣分。",
        ],
        evidence_note="来电者实际披露的年龄、受侵害和当前安全信息，受测者的询问方式、保密与保护说明、现实行动讨论，以及案例为模拟机构配置的具体工作程序。",
        anchors={
            0: "指责、怀疑或诱导来电者改变陈述；承诺绝对保密；把来电者重新推向已知危险且没有保护安排。",
            1: "只作一般安慰或仓促追问细节，对年龄适配、安全、保密边界和保护责任缺少处理。",
            2: "能保持基本尊重并了解部分安全信息，提出一般求助方向；对证据边界和机构程序掌握不完整。",
            3: "能以适龄方式了解必要事实，透明说明边界，判断当前安全并按机构程序形成保护和支持安排。",
            4: "面对来电者害怕后果、拒绝披露身份、家庭成员角色冲突或信息不完整时，仍能控制询问范围并形成最小必要、可解释的保护方案。",
        },
        conditional_in_level3=[],
    ),
}


_ALL_RUBRICS: dict[Target, CoreRubric | ModuleRubric] = {}
for _core_target, _core_rubric in _CORE_RUBRICS.items():
    _ALL_RUBRICS[_core_target] = _core_rubric
for _module_target, _module_rubric in _MODULE_RUBRICS.items():
    _ALL_RUBRICS[_module_target] = _module_rubric


def _replace_rubric_language(
    rubric: CoreRubric | ModuleRubric,
    replacements: tuple[tuple[str, str], ...],
) -> CoreRubric | ModuleRubric:
    payload = rubric.model_dump(mode="json")

    def replace(value: object) -> object:
        if isinstance(value, str):
            for old, new in replacements:
                value = value.replace(old, new)
            return value
        if isinstance(value, list):
            return [replace(item) for item in value]
        if isinstance(value, dict):
            return {key: replace(item) for key, item in value.items()}
        return value

    return type(rubric).model_validate(replace(payload))


def _online_c6(rubric: CoreRubric) -> CoreRubric:
    observations = {
        "C6.clarity": ("文字可理解性", "消息表达简明、指向清楚，避免长篇解释、术语堆叠和含糊指令"),
        "C6.turn_space": ("消息负荷与回应空间", "控制单条消息和连续提问的负荷，给来访者阅读、回应和补充的空间"),
        "C6.cue_adaptation": ("文字线索适配", "根据连续短消息、明显停顿、改口和文字内容调整回应长度与节奏"),
        "C6.interruption_handling": ("停顿与中断处理", "区分思考停顿、情绪停顿和技术中断，并以适当方式确认或恢复互动"),
        "C6.structure": ("互动结构", "开场、探索、工作和收束阶段基本清楚，使用简短概括与过渡维持方向"),
        "C6.time_use": ("时间使用", "保持当前重点，既不连续催促，也不让互动长期停留在重复内容中"),
    }
    return rubric.model_copy(
        update={
            "name": "文字表达与互动过程管理",
            "measures": "评价受测者能否以清楚、可跟随的文字和适当消息节奏组织在线互动，并处理连续短消息、明显停顿、信息负荷和阶段转换。",
            "indicators": [
                indicator.model_copy(
                    update={
                        "name": observations[indicator.id][0],
                        "observation": observations[indicator.id][1],
                    }
                )
                for indicator in rubric.indicators
            ],
            "excluded": [
                "打字速度、单条消息长度或消息数量本身。",
                "系统显示的输入状态或网络延迟本身。",
                "强烈情绪失控和长时无法交流的专项处理，归入S3。",
                "最终结束质量，归入C8。",
            ],
            "evidence_note": "使用完整文字互动、消息先后顺序、受测者的表达负荷及来访者的后续回应。系统输入状态和网络延迟不能单独作为能力或心理状态证据。",
            "anchors": {
                0: "持续使用明显压迫、嘲讽或失控的文字表达，或者在对方明确无法跟随时仍强行推进，导致互动无法继续。",
                1: "表达冗长或含糊，连续堆叠问题、频繁催促或忽略交流中断；互动缺少基本结构，来访者难以参与。",
                2: "文字基本清楚，能够完成普通消息往来；偶有节奏、负荷或阶段组织问题，但未持续破坏互动。",
                3: "能根据可靠的文字线索调整回应长度与节奏，合理处理停顿和中断，并以清楚的过渡和总结维持方向。",
                4: "面对表达反复、连续短消息、偶发技术中断或多次阶段变化时，仍能迅速恢复自然、稳定、有重点的文字互动。",
            },
            "conditional_in_level3": ["合理处理明显停顿、连续短消息和交流中断"],
        },
        deep=True,
    )


def get_rubric(
    target: Target,
    *,
    media: Media | str | None = None,
) -> CoreRubric | ModuleRubric:
    rubric = _ALL_RUBRICS[target].model_copy(deep=True)
    if media is None:
        return rubric
    selected_media = Media(media)
    neutral_replacements = (
        ("国家卫生健康委《心理援助热线技术指南（试行）》", "本次测评任务"),
        ("心理援助热线", "当前心理支持场域"),
        ("心理热线", "当前心理支持场域"),
        ("热线所属机构", "当前服务所属机构"),
        ("热线", "当前服务"),
        ("接线员", "受测者"),
        ("来电者", "来访者"),
        ("来电", "求助"),
        ("整通通话", "整段会谈"),
        ("通话", "会谈"),
    )
    if selected_media is Media.text:
        rubric = _replace_rubric_language(
            rubric,
            neutral_replacements + (("语音", "文字"), ("声音", "文字表达")),
        )
        if target is CoreDimension.voice_and_process:
            if not isinstance(rubric, CoreRubric):
                raise TypeError("C6 量规必须使用核心能力结构")
            return _online_c6(rubric)
        return rubric
    if target is CoreDimension.voice_and_process:
        return _replace_rubric_language(
            rubric,
            (("语音沟通与通话进程管理", "语音沟通与会谈过程管理"), ("通话", "会谈")),
        )
    if target in {
        CoreDimension.boundary_and_ethics,
        CoreDimension.closure_and_followup,
        *SpecialModule,
    }:
        return _replace_rubric_language(rubric, neutral_replacements)
    return rubric


def iter_core_rubrics() -> tuple[tuple[CoreDimension, CoreRubric], ...]:
    return tuple((target, rubric.model_copy(deep=True)) for target, rubric in _CORE_RUBRICS.items())


def iter_module_rubrics() -> tuple[tuple[SpecialModule, ModuleRubric], ...]:
    return tuple(
        (target, rubric.model_copy(deep=True)) for target, rubric in _MODULE_RUBRICS.items()
    )


def iter_rubrics() -> tuple[tuple[Target, CoreRubric | ModuleRubric], ...]:
    return tuple((target, rubric.model_copy(deep=True)) for target, rubric in _ALL_RUBRICS.items())
