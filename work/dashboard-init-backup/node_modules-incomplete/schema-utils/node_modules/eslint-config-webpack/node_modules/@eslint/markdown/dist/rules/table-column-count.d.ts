declare namespace _default {
    namespace meta {
        let type: "problem";
        namespace docs {
            let recommended: boolean;
            let description: string;
            let url: string;
        }
        namespace messages {
            let extraCells: string;
            let missingCells: string;
        }
        let schema: {
            type: "object";
            properties: {
                checkMissingCells: {
                    type: "boolean";
                };
            };
            additionalProperties: false;
        }[];
        let defaultOptions: [{
            checkMissingCells: false;
        }];
    }
    function create(context: import("@eslint/core").RuleContext<{
        LangOptions: import("../types.js").MarkdownLanguageOptions;
        Code: import("../index.js").MarkdownSourceCode;
        RuleOptions: TableColumnCountOptions;
        Node: import("mdast").Node | import("../language/markdown-source-code.js").InlineConfigComment;
        MessageIds: TableColumnCountMessageIds;
    }>): {
        table(node: import("mdast").Table): void;
    };
}
export default _default;
export type TableColumnCountMessageIds = "extraCells" | "missingCells";
export type TableColumnCountOptions = [{
    checkMissingCells?: boolean;
}];
export type TableColumnCountRuleDefinition = MarkdownRuleDefinition<{
    RuleOptions: TableColumnCountOptions;
    MessageIds: TableColumnCountMessageIds;
}>;
import type { MarkdownRuleDefinition } from "../types.js";
