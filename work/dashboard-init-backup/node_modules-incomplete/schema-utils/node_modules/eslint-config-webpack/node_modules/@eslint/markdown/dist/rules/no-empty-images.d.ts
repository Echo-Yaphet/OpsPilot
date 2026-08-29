declare namespace _default {
    namespace meta {
        let type: "problem";
        namespace docs {
            let recommended: boolean;
            let description: string;
            let url: string;
        }
        namespace messages {
            let emptyImage: string;
        }
    }
    function create(context: import("@eslint/core").RuleContext<{
        LangOptions: import("../types.js").MarkdownLanguageOptions;
        Code: import("../index.js").MarkdownSourceCode;
        RuleOptions: [];
        Node: import("mdast").Node | import("../language/markdown-source-code.js").InlineConfigComment;
        MessageIds: "emptyImage";
    }>): {
        image(node: import("mdast").Image): void;
    };
}
export default _default;
export type NoEmptyImagesMessageIds = "emptyImage";
export type NoEmptyImagesOptions = [];
export type NoEmptyImagesRuleDefinition = MarkdownRuleDefinition<{
    RuleOptions: NoEmptyImagesOptions;
    MessageIds: NoEmptyImagesMessageIds;
}>;
import type { MarkdownRuleDefinition } from "../types.js";
