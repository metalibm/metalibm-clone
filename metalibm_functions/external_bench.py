# -*- coding: utf-8 -*-

import sys

import sollya

from sollya import (
        S2, Interval, ceil, floor, round, inf, sup, pi, log, exp, cos, sin,
        guessdegree, dirtyinfnorm
)

from metalibm_core.core.ml_function import ML_Function, ML_FunctionBasis, DefaultArgTemplate

from metalibm_core.core.attributes import ML_Debug
from metalibm_core.core.ml_operations import *
from metalibm_core.core.ml_formats import *
from metalibm_core.core.precisions import ML_Faithful
from metalibm_core.code_generation.generic_processor import GenericProcessor
from metalibm_core.core.polynomials import *
from metalibm_core.core.ml_table import ML_NewTable
from metalibm_core.core.ml_complex_formats import ML_Mpfr_t
from metalibm_core.code_generation.generator_utility import FunctionOperator, FO_Result, FO_Arg
from metalibm_core.core.payne_hanek import generate_payne_hanek

from metalibm_core.utility.ml_template import ML_NewArgTemplate, ArgDefault, precision_parser
from metalibm_core.utility.log_report  import Log
from metalibm_core.utility.debug_utils import *
from metalibm_core.utility.num_utils   import ulp
from metalibm_core.utility.gappa_utils import is_gappa_installed



class ML_ExternalBench(ML_Function("ml_external_bench")):
  """ Implementation of external bench function wrapper """
  def __init__(self, args=DefaultArgTemplate):
    #arity = len(arg_template.input_precisions)
    # initializing base class
    ML_FunctionBasis.__init__(self, args)
    # initializing specific properties
    self.headers = args.headers
    self.libraries = args.libraries
    self.bench_function_name = args.bench_function_name
    self.emulate = args.emulate

  @staticmethod
  def get_default_args(**kw):
    """ Return a structure containing the arguments for ML_ExternalBench
        builtin from a default argument mapping overloaded with @p kw """
    default_args_exp = {
        "output_file": "bench.c",
        "function_name": "bench_wrapper",
        "precision": ML_Binary32,
        "accuracy": ML_Faithful,
        "target": GenericProcessor()
    }
    default_args_exp.update(kw)
    return DefaultArgTemplate(**default_args_exp)


  def generate_scheme(self): 
    # declaring CodeFunction and retrieving input variable
    inputs = [self.implementation.add_input_variable("x%d" % i, precision) for i,precision in enumerate(self.get_input_precisions())]

    external_function_op = FunctionOperator(self.bench_function_name, arity = self.get_arity(), output_precision = self.precision, require_header = self.headers)
    external_function = FunctionObject(self.bench_function_name, self.get_input_precisions(), self.precision, external_function_op)

    scheme = Statement(
      Return(
        external_function(*tuple(inputs)),
        precision = self.precision,
      )
    )

    return scheme

  def numeric_emulate(self, *args):
    return self.emulate(*args)

if __name__ == "__main__":
  # auto-test
  arg_template = ML_NewArgTemplate(default_arg=ML_ExternalBench.get_default_args())
  def precision_list_parser(s):
    return [precision_parser(p) for p in s.split(",")]

  # argument extraction
  arg_template.get_parser().add_argument(
    "--function", dest="bench_function_name", default="expf",
    action="store", type=str, help="name of the function to be benched")
  arg_template.get_parser().add_argument(
    "--headers", dest="headers", default=[], action="store",
    type=lambda s: s.split(","),
    help="comma separated list of required headers")
  arg_template.get_parser().add_argument(
    "--libraries", dest="libraries", default=[], action="store",
    type=lambda s: s.split(","),
    help="comma separated list of required libraries")

  def local_eval(s):
    return eval(s)
  arg_template.get_parser().add_argument(
    "--emulate", dest="emulate", default=lambda x: x, action="store",
    type=local_eval, help="function numeric emulation")

  args = arg_template.arg_extraction()
  ml_sincos = ML_ExternalBench(args)
  ml_sincos.gen_implementation()
