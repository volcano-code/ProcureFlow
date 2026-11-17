export function strictestBudget(requestBudget:string,policyCap:string|null):string;
export function bindingCurrent(binding:{current?:boolean;policy_hash?:string}|null|undefined,policyHash:string|null|undefined):boolean;
export function staleExplanation(reason:string|null|undefined):string;
export function violationExplanation(code:string):string;
