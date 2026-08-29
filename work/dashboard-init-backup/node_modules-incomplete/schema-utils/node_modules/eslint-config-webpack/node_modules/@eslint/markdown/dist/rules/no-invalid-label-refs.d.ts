declare namespace _default {
    namespace meta {
        let type: "problem";
        namespace docs {
            let recommended: boolean;
            let description: string;
            let url: string;
        }
        namespace messages {
            let invalidLabelRef: string;
        }
    }
    function create(context: import("@eslint/core").RuleContext<{
        LangOptions: import("../types.js").MarkdownLanguageOptions;
        Code: MarkdownSourceCode;
        RuleOptions: [];
        Node: import("mdast").Node | import("../language/markdown-source-code.js").InlineConfigComment;
        MessageIds: "invalidLabelRef";
    }>): {
        text(node: Text): void;
    };
}
export default _default;
export type NoInvalidLabelRefsMessageIds = "invalidLabelRef";
export type NoInvalidLabelRefsOptions = [];
export type NoInvalidLabelRefsRuleDefinition = MarkdownRuleDefinition<{
    RuleOptions: NoInvalidLabelRefsOptions;
    MessageIds: NoInvalidLabelRefsMessageIds;
}>;
import type { MarkdownSourceCode } from "../language/markdown-source-code.js";
import type { Text } from "mdast";
import type { MarkdownRuleDefinition } from "../types.js";
