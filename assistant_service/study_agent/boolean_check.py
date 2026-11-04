"""Bounded Boolean algebra verification. No eval, shell, HDL or file access."""
import itertools,re

def verify_boolean_design(variables,original,candidate):
    if not 1<=len(variables)<=6 or any(not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,15}',v) for v in variables):
        raise ValueError('变量为1至6个简短英文标识符')
    names=[v.casefold() for v in variables]
    if len(set(names))!=len(names) or set(names)&{'and','or','xor','not'}:raise ValueError('变量重复或与运算符同名')
    def parse(expression):
        if not 1<=len(expression)<=500:raise ValueError('表达式长度须为1至500')
        tokens=re.findall(r'[A-Za-z][A-Za-z0-9_]*|[01]|[()\x27!~&|^+*.]',expression)
        if ''.join(tokens)!=''.join(expression.split()):raise ValueError('包含不支持的字符；只解析布尔式，不执行代码')
        tokens=[t.casefold() for t in tokens];pos=0;depth=0
        def atom():
            nonlocal pos,depth
            if pos>=len(tokens):raise ValueError('表达式不完整')
            if tokens[pos] in ('not','!','~'):
                pos+=1;depth+=1
                if depth>32:raise ValueError('表达式嵌套过深')
                node=('not',atom());depth-=1
            elif tokens[pos]=='(':
                pos+=1;depth+=1
                if depth>32:raise ValueError('表达式嵌套过深')
                node=expression_or();depth-=1
                if pos>=len(tokens) or tokens[pos]!=')':raise ValueError('括号不配对')
                pos+=1
            else:
                t=tokens[pos];pos+=1
                if t in names:node=('var',names.index(t))
                elif t in ('0','1'):node=('const',int(t))
                else:raise ValueError('未声明变量或错误符号：'+t)
            while pos<len(tokens) and tokens[pos]=="'":node=('not',node);pos+=1
            return node
        def combine(part,operators,kind):
            nonlocal pos
            node=part()
            while pos<len(tokens) and tokens[pos] in operators:
                pos+=1;node=(kind,node,part())
            return node
        def expression_and():return combine(atom,('and','&','*','.'),'and')
        def expression_xor():return combine(expression_and,('xor','^'),'xor')
        def expression_or():return combine(expression_xor,('or','|','+'),'or')
        tree=expression_or()
        if pos!=len(tokens):raise ValueError('表达式未完整解析；乘积请显式用and或*连接')
        return tree
    def value(tree,bits):
        kind=tree[0]
        if kind=='var':return bits[tree[1]]
        if kind=='const':return tree[1]
        if kind=='not':return 1-value(tree[1],bits)
        a,b=value(tree[1],bits),value(tree[2],bits)
        return (a & b) if kind=='and' else (a ^ b) if kind=='xor' else (a | b)
    left,right=parse(original),parse(candidate);on_left=[];on_right=[];mismatches=[]
    for index,bits in enumerate(itertools.product((0,1),repeat=len(names))):
        a,b=value(left,bits),value(right,bits)
        if a:on_left.append(index)
        if b:on_right.append(index)
        if a!=b:mismatches.append({'index':index,'inputs':dict(zip(variables,bits)),'original':a,'candidate':b})
    return {'equivalent':not mismatches,'checked_rows':2**len(names),'variables_msb_first':variables,
            'original_on_set':on_left,'candidate_on_set':on_right,'counterexample':mismatches[0] if mismatches else None,
            'mismatching_rows':len(mismatches),
            'semantics':'布尔代数：NOT > AND > XOR > OR；支持后缀撇号。不是VHDL语法分析。',
            'scope':'只检查这些变量与二值输入的逻辑功能；不证明无毛刺、满足时序或HDL可综合。'}
