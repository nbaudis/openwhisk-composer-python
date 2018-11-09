# Copyright 2018 IBM Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import functools
import json
import marshal
import base64 
import types
import os
import inspect
import composer
import re
import requests
import traceback
from conductor import __version__

def escape(str):
    return re.sub(r'(\n|\t|\r|\f|\v|\\|\')', lambda m:{'\n':'\\n','\t':'\\t','\r':'\\r','^\f':'\\f','\v':'\\v','\\':'\\\\','\'':'\\\''}[m.group()], str)

def synthesize(composition): # dict
    code = '# generated by composer v'+composition['version']+' and conductor v'+__version__+'\n\nimport traceback\nimport os\nimport functools\nimport json\nimport inspect\nimport re\nimport base64\nimport marshal\nimport types\nimport requests\nimport urllib.parse'
    code += '\n\n' + inspect.getsource(composer.ComposerError)
    code += '\ncomposition=json.loads(\''+escape(str(composition['composition']))+'\')'

    print(str(composition['composition']))

    code += '\n' + inspect.getsource(conductor)
    code += '\n' + inspect.getsource(openwhisk)
    code += '\n' + inspect.getsource(Compositions)
    # code += '\n'+ src[src.index('def conductor'):]
    # code += '\ncombinators ='+ str(combinators)
    code += '\n' + inspect.getsource(composer.serialize)
    code += '\n' + inspect.getsource(composer.Composition)
    code += '\n' + inspect.getsource(composer.get_value)
    code += '\n' + inspect.getsource(composer.get_params)
    code += '\n' + inspect.getsource(composer.set_params)
    code += '\n' + inspect.getsource(composer.retain_result)
    code += '\n' + inspect.getsource(composer.retain_nested_result)
    code += '\n' + inspect.getsource(composer.dec_count)
    code += '\n' + inspect.getsource(composer.set_nested_params)
    code += '\n' + inspect.getsource(composer.get_nested_params)
    code += '\n' + inspect.getsource(composer.set_nested_result)
    code += '\n' + inspect.getsource(composer.get_nested_result)
    code += '\n' + inspect.getsource(composer.retry_cond)

    import openwhisk as ow
    code += '\n' + inspect.getsource(ow.Client)
    code += '\n' + inspect.getsource(ow.BaseOperation)
    code += '\n' + inspect.getsource(ow.Resource)
    code += '\n' + inspect.getsource(ow.Action)
    code += '\n' + inspect.getsource(ow.parse_id_and_ns)
    code += '\n' + inspect.getsource(ow.parse_id)
    code += '\n' + inspect.getsource(ow.parse_namespace)

    # code += '\n' + inspect.getsource(Compiler)
    code += 'def main(args):'
    code += '\n    return conductor(composition)(args)'

    annotations = [
        { 'key': 'conductor', 'value': str(composition['ast']) }, 
        { 'key': 'composerVersion', 'value': '0.16.1' }, #'value': composition['version'] },
        { 'key': 'conductorVersion', 'value': '0.16.1' } # 'value': __version__ }
    ]
  
    return { 'name': composition['name'], 'action': { 'exec': { 'kind': 'python:3', 'code':code }, 'annotations': annotations } }

def openwhisk(options):
    ''' return enhanced openwhisk client capable of deploying compositions '''
    
    # try to extract apihost and key first from whisk property file file and then from os.environ
    try:
        wskpropsPath = os.environ['WSK_CONFIG_FILE'] if 'WSK_CONFIG_FILE' in os.environ else os.path.expanduser('~/.wskprops')
        with open(wskpropsPath) as f:
            lines = f.readlines()

        options = dict(options)

        for line in lines:
            parts = line.strip().split('=')
            if len(parts) == 2:
                if parts[0] == 'APIHOST':
                    options['apihost'] = parts[1]
                elif parts[0] == 'AUTH':
                    options['api_key'] = parts[1]
    except:
        pass

    if '__OW_API_HOST' in os.environ:
        options['apihost'] = os.environ['__OW_API_HOST']

    if '__OW_API_KEY' in os.environ:
            options['api_key'] = os.environ['__OW_API_KEY']

    try:
        import openwhisk
        wsk = openwhisk.Client(options)
    except:
        wsk = Client(options)

    wsk.compositions = Compositions(wsk)
    return wsk

class Compositions:
    ''' management class for compositions '''
    def __init__(self, wsk):
        self.actions = wsk.actions
        
    def deploy(self, composition, overwrite):
        actions = composition['actions'] if 'actions' in composition else []
        actions.append(synthesize(composition))
        for action in actions:
            if overwrite:
                try:
                    self.actions.delete(action)
                except Exception:
                    pass
            self.actions.create(action)
        return actions

def conductor(composition): # main.
    wsk = None
    isObject = lambda x: isinstance(x, dict)
    
    # compile AST to FSM
    compiler = {}
    astnode = lambda f: compiler.setdefault(f.__name__[1:], f)

    @astnode
    def _sequence(parent, node):
        fsm = [{ 'parent': parent, 'type': 'pass' }]
        fsm.extend(compile(parent, *node['components']))
        return fsm

    @astnode
    def _action(parent, node):
        return [{ 'parent': parent, 'type': 'action', 'name': node['name'] }]

    @astnode
    def _asynchronous(parent, node):
        body = compile(parent, *node['components'])
        return [{ 'parent': parent, 'type': 'async', 'return': len(body) + 2}, *body, {'parent': parent, 'type': 'stop' }, {'parent': parent, 'type': 'pass' }]

    @astnode
    def _function(parent, node):
        return [{ 'parent': parent, 'type': 'function', 'exec': node['function']['exec'] }]

    @astnode
    def _ensure(parent, node):
        body = compile(parent, node['body'])
        finalizer = compile(parent, node['finalizer'])
        fsm = [{ 'parent': parent, 'type': 'try'}, *body, { 'parent': parent, 'type': 'exit' }, *finalizer]
        fsm[0]['catch'] = len(fsm) - len(finalizer)
        return fsm

    @astnode
    def _let(parent, node):
        body = compile(parent, *node['components'])
        return [{'parent': parent, 'type': 'let', 'let': node['declarations']}, *body, { 'parent': parent, 'type': 'exit' }]
    
    @astnode
    def _mask(parent, node):
        body = compile(parent, *node['components'])
        return [{'parent': parent, 'type': 'let', 'let': None}, *body, { 'parent': parent, 'type': 'exit' }]
    
    @astnode
    def _do(parent, node):
        handler = [ *compile(parent, node['handler']), {'parent': parent, 'type': 'pass'}]
        body = compile(parent, node['body'])
        fsm = [{ 'parent': parent, 'type': 'try' }, *body, { 'parent': parent, 'type': 'exit' }, *handler]
        fsm[0]['catch'] = len(fsm) - len(handler)
        fsm[len(fsm) - len(handler) - 1]['next'] = len(handler)
        return fsm
    
    @astnode
    def _when_nosave(parent, node):
        consequent = compile(parent, node['consequent'])
        alternate = [ *compile(parent, node['alternate']), { parent: 'parent', 'type': 'pass' }]
        fsm = [{ 'parent': parent, 'type': 'pass' }, 
            *compile(parent, node['test']), 
            { 'parent': parent, 'type': 'choice', 'then': 1, 'else': len(consequent) + 1 },
            *consequent, 
            *alternate]
        fsm[len(fsm) - len(alternate) - 1]['next'] = len(alternate)
        return fsm

    @astnode
    def _loop_nosave(parent, node):
        body = compile(parent, node['body'])
        test = compile(parent, node['test'])
        fsm = [{ 'parent': parent, 'type': 'pass' }, *test, 
            { 'parent': parent, 'type': 'choice', 'then': 1, 'else': len(body) + 1 },
            *body, { parent: 'parent', 'type': 'pass' }]
        fsm[len(fsm) - 2]['next'] = 2 - len(fsm) 
        return fsm
    
    @astnode
    def _doloop_nosave(parent, node):
        body = compile(parent, node['body'])
        test = compile(parent, node['test'])
        fsm = [{ 'parent': parent, 'type': 'pass' }, *body, *test,
               { 'parent': parent, 'type': 'choice', 'else': 1}, { parent: 'parent', 'type': 'pass' }]
        fsm[len(fsm) - 2]['then'] = 2 - len(fsm) 
        return fsm

    def compile(parent, *node):
        nonlocal compiler
        print("compile==\n")
        print(node)
        print("\n==compile\n")
        
        if len(node) == 0:
            return [{'parent': parent, 'type': 'empty'}]
        if len(node) == 1:
            return compiler[node[0]['type']](node[0]['path'] if 'path' in node[0] else parent, node[0])
        return functools.reduce(lambda fsm, node: extends(fsm, compile(parent, node)), node, [])
            

    def extends(l, items):
        l.extend(items)
        return l

    fsm = compile('', composition)
    print('FSM==\n')
    print(fsm)
    print('\n==FSM\n')

    conductor = {}
    operator = lambda f: conductor.setdefault(f.__name__[1:], f)
    
    @operator
    def _choice(p, node, index, inspect, step):
        p['s']['state'] = index + (node['then'] if p['params']['value'] else node['else'])
        return None
        
    @operator
    def _try(p, node, index, inspect, step):
        p['s']['stack'].insert(0, { 'catch': index + node['catch'] })

    @operator
    def _let(p, node, index, inspect, step):
        p['s']['stack'].insert(0, { 'let': node['let'] }) # JSON.parse(JSON.stringify(jsonv.let))
    
    @operator
    def _exit(p, node, index, inspect, step):
        if len(p['s']['stack']) == 0:
            return internalError('pop from an empty stack')
        p['s']['stack'].pop(0)

    @operator
    def _action(p, node, index, inspect, step):
        return { 'method': 'action', 'action': node['name'], 'params': p['params'], 'state': { '$resume': p['s'] } }

    @operator
    def _function(p, node, index, inspect, step):
        result = None
        try:
            functionName = node['exec']['functionName'] if 'functionName' in node['exec'] else None
            result = run(node['exec']['code'], p, node['exec']['kind'], functionName)
        except Exception as error:
            traceback.print_exc()
            print(error)
            result = { 'error': 'Function combinator threw an exception at AST node root'+node['parent']+' (see log for details)' }

        if callable(result):
            result = { 'error': 'Function combinator evaluated to a function type at AST node root'+node['parent']}

        # if a function has only side effects and no return value (or return None), return params
        p['params'] = p['params'] if result is None else result
        inspect_errors(p)
        return step(p)

    @operator
    def _empty(p, node, index, inspect, step):
        inspect_errors(p)

    @operator
    def _pass(p, node, index, inspect, step):
        pass

    @operator
    def _async(p, node, index, inspect, step):
        nonlocal wsk

        p['params']['$resume'] = { 'state': p['s']['state'], 'stack': [{ 'marker': True }] + p['s']['stack'] }
        p['s']['state'] = index + node['return']
        if wsk is None:
            wsk = openwhisk({ 'ignore_certs': True })
        try:
            response = wsk.actions.invoke({ 'name': os.getenv('__OW_ACTION_NAME'), 'params': p['params'] })
            result = { 'method': 'async', 'activationId': response['activationId'], 'sessionId': p['s']['session'] }
            
        except Exception as err:
            print(err) # invoke failed
            result = { 'error': 'Async combinator failed to invoke composition at AST node root'+node['parent']+' (see log for details)' }
    
        p['params'] = result
        inspect_errors(p)  
        return step(p)
        
    def finish(q):
        return q['params'] if 'error' in q['params'] else { 'params': q['params'] }

    def encodeError(error):
        if isinstance(error, str) or not hasattr(error, "__getitem__"):
            return {
                'code': 500,
                'error': error
            }
        else:
            return {
                'code': error['code'] if isinstance(error['code'], int) else 500,
                'error': error['error'] if isinstance(error['error'], str) else (error['message'] if 'message' in error else 'An internal error occurred')
            }

    # error status codes
    #badRequest = lambda error: { 'code': 400, 'error': error }
    internalError = lambda error: encodeError(error)

    def inspect_errors(p):
        if not isObject(p['params']):
            p['params'] = { 'value': p['params'] }
        if 'error' in p['params']:
            p['params'] = { 'error': p['params']['error'] } # discard all fields but the error field
            p['s']['state'] = -1 # abort unless there is a handler in the stack
            while len(p['s']['stack']) > 0 and 'marker' not in p['s']['stack'][0]:
                first = p['s']['stack'][0]
                p['s']['stack'] = p['s']['stack'][1:]
                if 'catch' in first:
                    p['s']['state'] = first['catch']
                    if p['s']['state'] >= 0:
                        break
 
    def reduceRight(func, init, seq):
        if not seq:
            return init
        else:
            return func(reduceRight(func, init, seq[1:]), seq[0])

    def update(dict, dict2):
        dict.update(dict2)
        return dict

    # run function f on current stack
    def run(f, p, kind, functionName=None):
        # handle let/mask pairs
        view = []
        n = 0
        for frame in p['s']['stack']:
            if 'let' in frame and frame['let'] is None:
                n += 1
            elif 'let' in frame:
                if n == 0:
                    view.append(frame)
                else:
                    n -= 1
        # update value of topmost matching symbol on stack if any
        def set(symbol, value):
            lets = [element for element in view if 'let' in element and symbol in element['let']]
            if len(lets) > 0:
                element = lets[0]
                element['let'][symbol] = value # TODO: JSON.parse(JSON.stringify(value))

        # collapse stack for invocation
        print("CODE=")
        print(f)
        env = reduceRight(lambda acc, cur: update(acc, cur['let']) if 'let' in cur and isinstance(cur['let'], dict) else acc, {}, view)
        if kind == 'python:3':
            main = '''exec(code + "\\n__out__['value'] = ''' + functionName + '''(env, args)", {'env': env, 'args': args, '__out__':__out__})'''
            code = f
        else: # lambda
            main = '''__out__['value'] = code(env, args)'''
            code = types.FunctionType(marshal.loads(base64.b64decode(bytearray(f, 'ASCII'))), {})

        try:
            out = {'value': None}
            print("EXEC==\n")
            print(code)
            exec(main, {'env': env, 'args': p['params'], 'code': code, '__out__': out})
            return out['value']
        finally:
            for name in env:
                set(name, env[name])

    def step(p):
        # final state, return composition result
        if p['s']['state'] < 0 or p['s']['state'] >= len(fsm):
            print('Entering final state')
            print(json.dumps(p['params']))
            return None

        # process one state
        node = fsm[p['s']['state']] # json definition for current state
        print('==NODE:')
        print(node)
        if 'path' in node:
            print('Entering composition'+node['path'])
        index = p['s']['state']
        print("index=", index)
        p['s']['state'] = p['s']['state'] + node.get('next', 1)
        print("node type=", node['type'])
        if not callable(conductor[node['type']]):
            return internalError('unexpected '+node['type']+' combinator')
        
        result = conductor[node['type']](p, node, index, inspect, step)
        print("--- set result ---")
        print(result)
        return result if result is not None else step(p)

    
    def invoke(params):
        ''' do invocation '''
        print(params)
        print("\n")
        resume = params.get('$resume', {})
        if '$resume' in params:
            del params['$resume']
        resume['session'] = resume.get('session', os.getenv('__OW_ACTIVATION_ID'))
        
        # current state
        s = { 'state': 0, 'stack': [], 'resuming': True }
        s.update(resume)
        p = { 's': s, 'params': params }
        
        if not isinstance(p['s']['state'], int):
            return internalError('state parameter is not a number')
        if not isinstance(p['s']['stack'], list):
            return internalError('stack parameter is not an array')
        
        if 'resuming' in resume:
            inspect_errors(p) # handle error objects when resuming

        result = None
        try: 
            result = step(p)
        except Exception as err:
            traceback.print_exc()
            print(err)
            p['params'] = {'error': internalError(err)}

        print("=== result ===")
        print(result)

        return result if result is not None else finish(p)
        
    return invoke        

