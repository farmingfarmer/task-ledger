"""How many utterances go into one extraction call.

ONE WINDOW IS ONE MODEL CALL, and this number is not a performance knob.
It changes what comes out.

Measured, on one transcript, one machine, one model. At 35 utterances per
call llama3.1:8b returned "Create synthetic data" and "Synthetic data is
almost done". At 8, from the same sentences, it returned "Synth kit
project", "Task Ledger Project" and "Smart problem list project" -- the
things the meeting was actually about. Given more context the model
summarises, and the specifics are what it drops. Nothing downstream can
recover a name that was never extracted.

The cost the smaller number is usually justified by -- more calls -- is not
even a cost here: attention grows faster than linearly with context, so
small windows are cheaper in total on CPU as well as more faithful. The
35-utterance windows also ran past the 1800s socket timeout and failed
outright.

DEFINED ONCE because it was defined four times. The ingesters said 35 and
quickscan said 8, so quickscan's preview disagreed with the pipeline it was
built to predict -- and the disagreement was read as a pipeline bug for
most of a day. Two numbers that must agree should not be two numbers.
"""

DEFAULT_WINDOW_ITEMS = 8
