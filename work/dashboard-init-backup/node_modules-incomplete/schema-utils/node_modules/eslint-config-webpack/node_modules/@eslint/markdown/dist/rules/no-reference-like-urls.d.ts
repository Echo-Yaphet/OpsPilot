declare namespace _default {
    namespace meta {
        let type: "problem";
        namespace docs {
            let recommended: boolean;
            let description: string;
            let url: string;
        }
        let fixable: "code";
        namespace messages {
            let referenceLikeUrl: string;
        }
    }
    function create(context: import("@eslint/core").RuleContext<{
        LangOptions: import("../types.js").MarkdownLanguageOptions;
        Code: import("../index.js").MarkdownSourceCode;
        RuleOptions: [];
        Node: import("mdast").Node | import("../language/markdown-source-code.js").InlineConfigComment;
        MessageIds: "referenceLikeUrl";
    }>): {
        definition(node: import("mdast").Definition): void;
        "image, link"(node: Image | Link): void;
        "root:exit"(): void;
    };
}
export default _default;
export type NoReferenceLikeUrlsMessageIds = "referenceLikeUrl";
export type NoReferenceLikeUrlsOptions = [];
export type NoReferenceLikeUrlsRuleDefinition = MarkdownRuleDefinition<{
    RuleOptions: NoReferenceLikeUrlsOptions;
    MessageIds: NoReferenceLikeUrlsMessageIds;
}>;
import type { Image } from "mdast";
import type { Link } from "mdast";
import type { MarkdownRuleDefinition } from "../types.js";
