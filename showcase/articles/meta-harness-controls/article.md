# Before we let an agent optimize the harness

*We measured ordinary prompting, labeled examples and repeated sampling while preparing a Meta-Harness experiment. The largest gain came from the examples. Extra sampling exposed a separate problem: sometimes the model produced a correct answer and the voting rule threw it away.*

In our [Harness² recreation](https://forge.coreweave.com/wandb/wandb/harness2-agent-lens-v4/reports/Recreating-Harness%C2%B2--VmlldzoxODE5OTAxNQ), an agent wrote a checking script, repaired the instructions for finding it, tried another revision and selected the earlier version. We could follow those changes into the next solver execution. On fresh coding tasks, though, we didn't establish an average improvement over the original harness.

That distinction carries into our next experiment. Before giving an agent credit for improving its harness, we need to know what it improved on. A weak starting prompt, a fortunate model response and a larger sampling budget can each make a new version look promising.

While preparing a separate study using Meta-Harness's text-task example, we measured those alternatives. We ran two supplied baselines, repeated unchanged requests, and tested a five-answer voting policy. We connected execution and accounting to Forge so we could check the individual attempts behind the scores.

This article covers those completed controls. We haven't completed Meta-Harness's agent-driven search, so there is no optimized harness result here. The model and tasks also differ from our Harness² study; these numbers don't explain or revise its coding results.

## What the editor would have to beat

We used `gpt-oss-120b` through W&B Inference on three released task sets: predicting reactants from a product in USPTO, matching symptom descriptions to disease labels in Symptom2Disease, and predicting charge labels in LawBench. The test sets contain 100, 212 and 100 tasks respectively.

The simplest baseline sends the problem directly to the model and extracts its final answer. The second places labeled training examples in the prompt and asks the model to follow their patterns. It draws from 50 training examples for USPTO and 200 for each other dataset, with a 30,000-character cap on the examples included in a request. The examples' order is determined from the input, so repeating a task preserves that order. The model's weights don't change.

The third setup uses that same example-based prompt to generate five fresh answers. It groups normalized answer labels, chooses the most common, and breaks a tie by taking the earliest sample in a tied group. We grade the selected original answer. This is a vote over model outputs; it has no access to the correct answer when choosing.

Here is what happened on the same 412 test tasks:

| Setup | Temperature | Answers per task | Mean dataset accuracy | Marginal inference cost |
|---|---:|---:|---:|---:|
| Direct prompt | 0 | 1 | 28.97% | $0.0553 |
| Labeled examples | 0 | 1 | 43.16% | $0.1874 |
| Labeled examples + voting | 1 | 5 | 45.83% | $0.9529–$1.0083 |

*Accuracy gives each dataset equal weight; it isn't the fraction correct after pooling all 412 tasks. Costs use recorded token usage and the prices frozen for this experiment. The voting range includes a conservative allowance for missing usage. These are marginal test-inference estimates, excluding compute, preparation, earlier replayed calls and harness search—not invoices or the total cost of the project.*

The example-based baseline gained **14.19 percentage points** over the direct prompt. A paired task bootstrap gave a 95% interval of **10.24 to 18.23 points**, conditional on these datasets and saved model outputs. Across tasks, 71 answers changed from wrong to correct and 11 went the other way. Both setups failed on 178 tasks.

Every dataset's score rose: USPTO from 13/100 to 18/100, Symptom2Disease from 144/212 to 177/212, and LawBench from 6/100 to 28/100. The gain belongs to these baseline implementations, including their different instructions and example context. We didn't isolate the effect of examples from the surrounding prompt wording.

For a future harness search, I would use the example-based baseline as the main comparator. Beating the direct prompt alone would leave a large, already demonstrated alternative unexplained.

## We need to know what “correct” means

Before interpreting that gain, we checked the scoring code and regraded the saved predictions. These tasks have deterministic scorers, but they measure particular answer conventions.

The USPTO scorer compares sets of dot-separated reactant strings after lowercasing. Symptom2Disease extracts and normalizes a disease label before matching it. LawBench compares sets of extracted charge labels. These checks don't establish chemical equivalence, clinical validity or legal correctness.

That limits the claim we can make about examples. They may help the model solve a problem, match the expected naming convention, or both. The measured gain is agreement with the frozen scorers. We haven't separated those mechanisms, and changing the scorer after seeing the outputs would answer a different question.

We also found one exact validation–test overlap in Symptom2Disease. Removing it leaves 411 test tasks and changes the examples-versus-direct difference from 14.19 to **14.21 points**. We retain the declared 412-task result and report that sensitivity separately. These are released benchmark examples, with no claim that they were absent from the model's training data.

## Repeating the same request changed the result

A search process chooses between candidate harnesses based on measured scores. If a score can move without a harness edit, a small apparent improvement needs another explanation before it gets credited to the editor.

We ran each baseline five times on 130 validation tasks: 30 USPTO, 50 Symptom2Disease and 50 LawBench. That creates 260 task-and-baseline groups and 1,300 scored outputs. Every group had identical serialized requests across its five repeats, including temperature zero. We did not supply a model API seed; the dataset loader's seed controls task ordering, not provider execution.

Correctness changed across repeats in **31 of the 260 groups**. The normalized answer label changed in 126 groups. Those are different measurements: a model can produce several different wrong answers without changing its score.

![Five validation repeats show the direct-prompt mean ranging from 22.67% to 25.33%, and the example-based mean from 40.89% to 43.78%, despite unchanged requests.](assets/validation-repeatability.png)

*The dots are five observed repeats, not five new task sets. The range describes this run; it is not a confidence interval. Validation scores use a different task set from the test results above.*

The example-based validation mean ranged from 40.89% to 43.78%; the direct-prompt mean ranged from 22.67% to 25.33%. We haven't identified the provider-level cause of that variation. We have established that, in this setup, setting temperature to zero did not make repeated outputs or scores identical.

The task-bootstrap intervals in this article hold the saved model outputs fixed. They do not include this additional variation from rerunning the model, or the uncertainty introduced by searching over many harness candidates. Five repeats describe a baseline we can observe; they aren't a universal noise threshold for accepting edits.

## Five answers cost more. The gain was uncertain.

The voting policy reached 45.83%, **2.67 points** above the single example-based answer. Its paired 95% interval was **−0.20 to +5.65 points**. We haven't established a gain under this comparison, and an interval crossing zero doesn't establish equivalence either.

Voting fixed 19 tasks that the single-answer baseline missed and broke 11 it had answered correctly. On Symptom2Disease, both scored 177/212, but they didn't succeed on exactly the same tasks: voting gained six and lost six.

![Voting versus one example-based answer shows gains, losses and shared failures for each dataset; the overall paired difference is 2.67 points with an interval spanning zero.](assets/voting-paired.png)

*Each comparison follows the same task across the two policies. Both policies were wrong on 170 tasks. The aggregate difference hides the changes that cancel out, especially on Symptom2Disease.*

This comparison changes both the number of samples and temperature. As a secondary analysis of the saved five-sample runs, we also compared the vote with the first temperature-one sample. The advantage shrank to **1.33 points**, with an interval of **−1.02 to +3.82**. That first sample was fixed by its position, not selected for its score; it is still part of the same five draws.

The voting run used **5.08–5.38 times** the marginal inference cost of the single example-based answer. Fourteen provider attempts ended without complete usage, so we preserve their possible cost in the upper bound. Retrying them doesn't make the failed attempts free. The campaigns also overlapped and encountered throttling, so we don't use their wall times to claim a latency advantage.

I wouldn't choose five-answer voting as the default from this evidence. It remains a comparison an optimizer should face if the proposed harness also spends extra inference per task.

## Sometimes the correct answer was already there

We kept all five outputs for every task, including the four that the vote didn't select. That lets us distinguish two problems a final accuracy number combines.

On **156 of 412 tasks**, all five answers were wrong under the native scorer. A selector restricted to those answers had no correct answer available. On another **25 tasks**, at least one sample was correct but the vote selected a wrong answer.

| Dataset | All five answers wrong | Correct answer available, vote missed it |
|---|---:|---:|
| USPTO · 100 tasks | 76 | 4 |
| Symptom2Disease · 212 tasks | 20 | 15 |
| LawBench · 100 tasks | 60 | 6 |

There were also **33 tasks where all five samples agreed on the same normalized label and that answer was wrong**. Agreement alone did not settle correctness.

These counts suggest different experiments. When every candidate is wrong, changing only the selector cannot recover a correct answer from that set. When a correct candidate exists, we can compare selectors on the same saved candidates and give them equal budgets. The 25 missed answers identify an opportunity to investigate selection; they don't show that an implementable selector can recognize those answers without looking at the labels.

This connects to the question left by our Harness² walkthrough, where the editor preferred an earlier patch partly because of a visible hint about change size. Proposing candidates and recognizing the strongest candidate need separate tests. Our voting experiment measures that distinction in a simpler setting; it doesn't validate a selector for coding patches.

## What Forge let us check

We used CoreWeave Sandboxes for task execution, W&B Inference for solver calls, Agent Lens for stored execution traces, and W&B runs, Tables and artifacts for the results and their versions. The worker returned an answer and its task identity; our runner joined that result to the provider attempt, applied the frozen scorer, and kept the link through export.

That join mattered during recovery. One repeat failed after 49 of its 50 calls had completed. The retry reused those 49 saved responses and made the remaining call. We counted the reused responses as task outcomes and preserved the original attempts in the cost ledger. A resumed task result isn't another fresh model draw.

We also saw HTTP-success responses whose streams were incomplete. Their status codes didn't establish that we had a complete answer or complete usage. We kept those attempts and their cost bounds separate from the successful retries used in the final comparison.

For the repeatability and voting campaign, we verified all **53 export units: 48 Agent Lens exports and five W&B exports**. We read back the saved trace records, native run metrics, configuration lineage and table cells. This checks that the stored evidence matches what we exported. It doesn't validate the semantic meaning of the benchmark's labels.

Native EvalTables creation failed in this campaign. We recorded an explicit change to use W&B Tables, then verified that fallback. The Harness² report's working EvalTables panels are evidence from its own campaign; we aren't borrowing them to imply that the new integration succeeded. The new experiment's detailed traces remain access controlled, while the calculation download below contains scores and aggregates without task text or answer labels.

## The next harness experiment

These controls give us a stronger starting point for agent-driven search. The example-based prompt is the primary baseline. Extra inference needs a comparable sampling control. Small validation gains need repeated measurements, and a selector needs evaluation separate from the generator that produced its candidates.

We have now inspected these released test outcomes, so I would not use them as an untouched final evaluation while tuning the next method. I'd develop the candidate and selector policies on development data, freeze them, and evaluate on a fresh reserved set with repeated runs. The retained attempt records would let us distinguish a changed procedure from a rerun that happened to go better.

## Inspect the calculations

The [analysis download](assets/analysis.zip) contains paired correctness rows, validation-repeat outcomes, aggregate voting diagnostics, cost estimates and a small standard-library reproduction script. It recomputes the headline means and paired intervals without model calls. It doesn't contain the task prompts, target labels, private traces or raw model responses.

The setup follows the pinned [Meta-Harness text-task example](https://github.com/stanford-iris-lab/meta-harness/tree/f805ad3aa4ada290458c1e0deadbe132fbbdea4e/reference_examples/text_classification). Our scorer and data snapshots stayed fixed across these controls. The comparison is specific to this solver, provider, task selection and prompt implementations; testing a new editor or a new model requires another experiment.
