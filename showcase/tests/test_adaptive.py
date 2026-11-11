import itertools, threading, time, unittest
from concurrent.futures import ThreadPoolExecutor
from adaptive_core import choose,can_stop,execute_policy,schedule,request_for

class PolicyTest(unittest.TestCase):
    def test_exhaustive_stopping_preserves_fixed_five(self):
        for labels in itertools.product(('', 'a','b'),repeat=5):
            made=[]
            def batch(pos):
                made.extend(pos)
                return [dict(position=p,label=labels[p-1],ok=True) for p in pos]
            samples,selected,status=execute_policy('stop3_else5',batch)
            self.assertEqual(status,'complete')
            self.assertEqual(selected,choose(labels))
            self.assertEqual(len(made),3 if labels[0] and labels[0]==labels[1]==labels[2] else 5)
    def test_transport_failure_is_retained_not_early_success(self):
        made=[]
        def batch(pos):
            made.extend(pos);return [dict(position=p,label='a',ok=p!=2) for p in pos]
        s,i,status=execute_policy('stop3_else5',batch)
        self.assertEqual((made,i,status),([1,2,3],None,'provider_failure'))
    def test_batches_concurrent_but_followup_is_conditional(self):
        events=[];lock=threading.Lock();barrier=threading.Barrier(3)
        with ThreadPoolExecutor(max_workers=5) as pool:
            def one(p):
                with lock:events.append(('start',p,time.monotonic()))
                if p<4:barrier.wait(timeout=3)
                time.sleep((6-p)*.003)
                with lock:events.append(('end',p,time.monotonic()))
                return dict(position=p,label=str(p%2),ok=True)
            def batch(pos):
                fs=[pool.submit(one,p) for p in pos]
                return [f.result() for f in fs]
            samples,selected,status=execute_policy('stop3_else5',batch)
        self.assertEqual([s['position'] for s in samples],[1,2,3,4,5])
        first_ends=[t for k,p,t in events if k=='end' and p<=3]
        next_starts=[t for k,p,t in events if k=='start' and p>3]
        self.assertLessEqual(max(first_ends),min(next_starts))
        self.assertEqual(selected,0)
    def test_request_copied_without_input_mutation(self):
        task={'request':{'temperature':0,'messages':[{'content':'x'}]}}
        r=request_for(task,1);r['messages'][0]['content']='y'
        self.assertEqual(task['request']['messages'][0]['content'],'x')
        self.assertEqual(task['request']['temperature'],0)
    def test_schedule_balanced_adjacent_and_deterministic(self):
        tasks={'a':{str(i):{} for i in range(33)},'b':{'0':{}}};s=schedule(tasks)
        self.assertEqual(s,schedule(tasks));self.assertEqual(sum(len(b['indices']) for b in s),34*8)
        for i in range(0,len(s),2):
            self.assertEqual(s[i]['pair_id'],s[i+1]['pair_id']);self.assertEqual(s[i]['indices'],s[i+1]['indices']);self.assertNotEqual(s[i]['policy'],s[i+1]['policy'])
    def test_reordered_positions_rejected(self):
        with self.assertRaises(ValueError):execute_policy('parallel5',lambda ps:[dict(position=p,ok=True,label='a') for p in reversed(ps)])
if __name__=='__main__':unittest.main()
