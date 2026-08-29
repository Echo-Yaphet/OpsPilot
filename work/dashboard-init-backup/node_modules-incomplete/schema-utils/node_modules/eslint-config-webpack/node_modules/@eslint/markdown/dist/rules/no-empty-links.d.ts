declare namespace _default {
    namespace meta {
        let type: "problem";
        namespace docs {
            let recommended: boolean;
            let description: string;
            let url: string;
        }
        namespace messages {
            let emptyLink: string;
        }
    }
    function create(context: import("@eslint/core").RuleContext<{
        LangOptions: import("../types.js").MarkdownLanguageOptions;
        Code: import("../index.js").MarkdownSourceCode;
        RuleOptions: [];
        Node: import("mdast").Node | import("../language/markdown-source-code.js").InlineConfigComment;
        MessageIds: "emptyLink";
    }>): {
        link(node: import("mdast").Link): void;
    };
}
export default _default;
export type NoEmptyLinksMessageIds = "emptyLink";
export type NoEmptyLinksOptions = [];
export type NoEmptyLinksRuleDefinition = MarkdownRuleDefinition<{
    RuleOptions: NoEmptyLinksOptions;
    MessageIds: NoEmptyLinksMessageIds;
}>;
import type { MarkdownRuleDefinition } from "../types.js";
