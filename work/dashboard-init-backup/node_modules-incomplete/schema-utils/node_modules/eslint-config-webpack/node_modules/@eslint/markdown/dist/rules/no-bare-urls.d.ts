declare namespace _default {
    namespace meta {
        let type: "problem";
        namespace docs {
            let description: string;
            let url: string;
        }
        let fixable: "code";
        namespace messages {
            let bareUrl: string;
        }
    }
    function create(context: import("@eslint/core").RuleContext<{
        LangOptions: import("../types.js").MarkdownLanguageOptions;
        Code: import("../index.js").MarkdownSourceCode;
        RuleOptions: [];
        Node: import("mdast").Node | import("../language/markdown-source-code.js").InlineConfigComment;
        MessageIds: "bareUrl";
    }>): {
        ":matches(heading, paragraph, tableCell) html"(node: Html): void;
        ":matches(heading, paragraph, tableCell) link"(node: Link): void;
        "heading:exit"(): void;
        "paragraph:exit"(): void;
        "tableCell:exit"(): void;
        "root:exit"(): void;
    };
}
export default _default;
export type NoBareUrlsMessageIds = "bareUrl";
export type NoBareUrlsOptions = [];
export type NoBareUrlsRuleDefinition = MarkdownRuleDefinition<{
    RuleOptions: NoBareUrlsOptions;
    MessageIds: NoBareUrlsMessageIds;
}>;
import type { Html } from "mdast";
import type { Link } from "mdast";
import type { MarkdownRuleDefinition } from "../types.js";
