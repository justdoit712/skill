"""测试需求澄清多选解析与完整语义传递。"""

import unittest
from src.finder.run import parse_clarification_choice
from src.finder.plan import build_interactive_plan_prompt


class TestClarificationChoiceParsing(unittest.TestCase):
    def setUp(self):
        self.options = [
            "用于 Python / TypeScript 代码生成与单元测试重构",
            "用于日常技术文档与中英文文章润色写作",
            "通用的 LLM 提示词模板设计与效果评估",
            "面向特定业务领域（如法律/金融）的问答微调",
        ]

    def test_single_choice(self):
        res = parse_clarification_choice("2", self.options)
        self.assertEqual(res["input_type"], "single_choice")
        self.assertEqual(res["selected_indices"], [2])
        self.assertEqual(res["selected_texts"], [self.options[1]])
        self.assertEqual(res["answer"], self.options[1])

    def test_multi_choice_comma_english(self):
        res = parse_clarification_choice("1,3", self.options)
        self.assertEqual(res["input_type"], "multiple_choice")
        self.assertEqual(res["selected_indices"], [1, 3])
        self.assertEqual(res["selected_texts"], [self.options[0], self.options[2]])
        self.assertEqual(res["answer"], f"{self.options[0]}；{self.options[2]}")

    def test_multi_choice_comma_chinese(self):
        res = parse_clarification_choice("1，2，3", self.options)
        self.assertEqual(res["input_type"], "multiple_choice")
        self.assertEqual(res["selected_indices"], [1, 2, 3])
        self.assertEqual(res["selected_texts"], self.options[:3])
        self.assertEqual(res["answer"], "；".join(self.options[:3]))

    def test_multi_choice_mixed_delimiters(self):
        # 涵盖顿号、英文逗号、中文逗号、分号与空格混合输入：1,2,3，4
        res = parse_clarification_choice("1, 2、3，4；", self.options)
        self.assertEqual(res["input_type"], "multiple_choice")
        self.assertEqual(res["selected_indices"], [1, 2, 3, 4])
        self.assertEqual(res["selected_texts"], self.options)
        self.assertEqual(res["answer"], "；".join(self.options))

    def test_multi_choice_deduplication(self):
        res = parse_clarification_choice("1, 2, 1, 2", self.options)
        self.assertEqual(res["input_type"], "multiple_choice")
        self.assertEqual(res["selected_indices"], [1, 2])
        self.assertEqual(res["selected_texts"], [self.options[0], self.options[1]])

    def test_out_of_bounds_filtering(self):
        # 选项只有 4 项，输入 1, 5, 9
        res = parse_clarification_choice("1, 5, 9", self.options)
        self.assertEqual(res["input_type"], "single_choice")
        self.assertEqual(res["selected_indices"], [1])
        self.assertEqual(res["selected_texts"], [self.options[0]])
        self.assertEqual(res["answer"], self.options[0])

    def test_all_out_of_bounds_falls_back_to_free_text(self):
        # 全部编号无效时保留原始文本
        res = parse_clarification_choice("8, 9", self.options)
        self.assertEqual(res["input_type"], "free_text")
        self.assertEqual(res["selected_indices"], [])
        self.assertEqual(res["selected_texts"], [])
        self.assertEqual(res["answer"], "8, 9")

    def test_free_text_preserved(self):
        user_text = "需要专精于医疗问诊的多轮情感陪伴对话，语气必须温和克制"
        res = parse_clarification_choice(user_text, self.options)
        self.assertEqual(res["input_type"], "free_text")
        self.assertEqual(res["selected_indices"], [])
        self.assertEqual(res["selected_texts"], [])
        self.assertEqual(res["answer"], user_text)
        self.assertEqual(res["answer_raw"], user_text)

    def test_empty_input(self):
        res = parse_clarification_choice("", self.options)
        self.assertEqual(res["input_type"], "empty")
        self.assertEqual(res["answer"], "")
        self.assertEqual(res["selected_texts"], [])

    def test_build_interactive_plan_prompt_with_selected_texts(self):
        history = [
            {
                "turn": "1",
                "focus": "场景细化",
                "question": "应用场景？",
                "answer_raw": "1,3",
                "options": self.options,
                "selected_indices": [1, 3],
                "selected_texts": [self.options[0], self.options[2]],
                "answer": f"{self.options[0]}；{self.options[2]}",
                "input_type": "multiple_choice",
            },
            {
                "turn": "2",
                "focus": "特定需求",
                "question": "是否有其他约束？",
                "answer_raw": "不需要依赖外部平台",
                "options": [],
                "selected_indices": [],
                "selected_texts": [],
                "answer": "不需要依赖外部平台",
                "input_type": "free_text",
            },
        ]
        system, user = build_interactive_plan_prompt("代码重构助手", history)
        # 确保选项全文而非孤立数字出现在 prompt 中
        self.assertIn("用户确认（多选）：" + self.options[0] + "；" + self.options[2], user)
        self.assertIn("用户确认：不需要依赖外部平台", user)
        self.assertNotIn("1,3", user)


if __name__ == "__main__":
    unittest.main()
