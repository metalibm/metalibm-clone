# -*- coding: utf-8 -*-

###############################################################################
# This file is part of metalibm (https://github.com/kalray/metalibm)
###############################################################################
# MIT License
#
# Copyright (c) 2018 Kalray
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
###############################################################################

###############################################################################
# created:
# last-modified:    Mar  7th, 2018
#
# author(s): Nicolas Brunie (nicolas.brunie@kalray.eu)
###############################################################################

import os
import random
import subprocess
import re

try:
    # matplotlib import is optionnal (using for plotting only)
    import matplotlib.pyplot as matplotlib
except:
    matplotlib = None

from sollya import inf, sup, Interval

from metalibm_core.core.ml_formats import *
from metalibm_core.core.ml_optimization_engine import OptimizationEngine
from metalibm_core.core.ml_operations import *
from metalibm_core.core.ml_table import ML_NewTable
from metalibm_core.core.ml_complex_formats import ML_Mpfr_t
from metalibm_core.core.ml_call_externalizer import (
    CallExternalizer, generate_function_from_optree
)
from metalibm_core.core.ml_vectorizer import (
    StaticVectorizer, no_scalar_fallback_required
)
from metalibm_core.core.precisions import *
from metalibm_core.core.random_gen import get_precision_rng

from metalibm_core.code_generation.code_object import (
    NestedCode, CodeObject, LLVMCodeObject, MultiSymbolTable
)
from metalibm_core.code_generation.code_function import (
    CodeFunction, FunctionGroup
)
from metalibm_core.code_generation.generic_processor import GenericProcessor
from metalibm_core.code_generation.mpfr_backend import MPFRProcessor
from metalibm_core.code_generation.c_code_generator import CCodeGenerator
from metalibm_core.code_generation.llvm_ir_code_generator import LLVMIRCodeGenerator
from metalibm_core.code_generation.code_constant import C_Code
from metalibm_core.core.passes import (
    Pass, PassScheduler, PassDependency, AfterPassById,
)


from metalibm_core.code_generation.gappa_code_generator import GappaCodeGenerator

from metalibm_core.utility.log_report import Log
from metalibm_core.utility.debug_utils import *
from metalibm_core.utility.ml_template import DefaultArgTemplate
from metalibm_core.utility.build_utils import SourceFile, get_cmd_stdout




## \defgroup ml_function ml_function
## @{

def execute_pass_on_fct_group(scheduler, pass_object, function_group):
    """ execute an optimization pass on a function_group 
        :param scheduler: pass scheduler
        :type scheduler: PassScheduler
        :param pass_object: pass to be executed 
        :type pass_object: Pass
        :param function_group: group of functions on which the pass object
                               will be executed 
        :return: pass execution result
        :rtype: bool
    """
    return pass_object.execute_on_fct_group(function_group)

class BuildError(Exception):
    """ Exception to indicate that a build stage failed """
    pass
class ValidError(Exception):
    """ Exception to indicate that a validation stage failed """
    pass


## standardized function name geneation
#  @param base_name string name of the mathematical function
#  @param io_precisions list of output, input formats (outputs followed by inputs)
#  @param in_arity integer number of input arguments
#  @param out_arity integer number of function results
def libc_naming(base_name, io_precisions, in_arity = 1, out_arity = 1):
  precision_map = {
    ML_Binary32: "sf",
    ML_Binary64: "df",
    ML_Int32: "si",
    ML_Int64: "sd",
    ML_UInt32: "ui",
    ML_UInt64: "ud"
  }
  format_list = [io_precisions[0]] * (in_arity + out_arity) if len(io_precisions) == 1 else io_precisions
  format_suffix = ""
  previous_format = None
  counter = 0
  for precision in format_list:
    if precision != previous_format:
      if counter != 0:
        format_suffix += "%s%d" % (precision_map[previous_format], counter)
      counter = 0
      previous_format = precision
    counter += 1
  return base_name + format_suffix


def vector_elt_assign(vector_node, elt_index, elt_value):
    """ generate an assignation to set vector_node[index] <= elt_value """
    if isinstance(vector_node.get_precision(), ML_MultiPrecision_VectorFormat):
        def set_limb_precision(limb, vector_limb):
            """ define the precision of @p limb to match @p vector_limb """
            limb.set_precision(vector_limb.precision.get_scalar_format())
        assign_list = []
        hi_limb = elt_value.hi
        set_limb_precision(hi_limb, vector_node.hi)
        assign_list.append(ReferenceAssign(
            VectorElementSelection(vector_node.hi, elt_index),
            hi_limb
        ))
        lo_limb = elt_value.lo
        set_limb_precision(lo_limb, vector_node.lo)
        assign_list.append(ReferenceAssign(
            VectorElementSelection(vector_node.lo, elt_index),
            lo_limb
        ))
        if vector_node.get_precision().limb_num > 2:
            me_limb = elt_value.me
            set_limb_precision(me_limb, vector_node.me)
            assign_list.append(ReferenceAssign(
                VectorElementSelection(vector_node.me, elt_index),
                me_limb
            ))
    else:
        assign_list = [ReferenceAssign(
            VectorElementSelection(vector_node, elt_index), elt_value
        )]
    return assign_list


def generate_vector_implementation(scalar_scheme, scalar_arg_list,
                                   vector_size, name_factory, precision):
    """ generate a vector implementation of @p scalar_scheme """
    # declaring optimizer
    vectorizer = StaticVectorizer(self.opt_engine)

    callback_name = self.uniquify_name("scalar_callback")

    scalar_callback_function = generate_function_from_optree(name_factory, scalar_scheme, scalar_arg_list, callback_name, precision)
    # adding static attributes
    scalar_callback_function.add_attribute("static")

    Log.report(Log.Info, "[SV] optimizing Scalar scheme")
    scalar_scheme = self.optimise_scheme(scalar_scheme)
    scalar_scheme.set_tag("scalar_scheme")

    scalar_callback          = scalar_callback_function.get_function_object()

    # Call externalizer engine
    call_externalizer = CallExternalizer(self.get_main_code_object())

    Log.report(Log.Info, "[SV] vectorizing scheme")
    sub_vector_size = self.processor.get_preferred_sub_vector_size(self.precision, vector_size) if self.sub_vector_size is None else self.sub_vector_size
    vec_arg_list, vector_scheme, vector_mask = \
        vectorizer.vectorize_scheme(scalar_scheme, scalar_arg_list,
                                    vector_size, sub_vector_size)

    vector_output_format = vectorizer.vectorize_format(output_precision,
                                                       vector_size)


    Log.report(Log.Info, "vector_output_format is {}".format(vector_output_format))
    Log.report(Log.Info, "vector_mask is {}".format(vector_mask))
    vec_res = Variable("vec_res", precision=vector_output_format,
                       var_type=Variable.Local)


    vector_mask.set_attributes(tag="vector_mask", debug=debug_multi)


    if self.language in [C_Code, OpenCL_Code]:
        self.get_main_code_object().add_header("support_lib/ml_vector_format.h")

    Log.report(Log.Info, "[SV] building vectorized main statement")
    if no_scalar_fallback_required(vector_mask):
        function_scheme = Statement(
            Return(vector_scheme, precision=vector_output_format)
        )
    elif self.language is OpenCL_Code:
        function_scheme = generate_opencl_vector_wrapper(self.main_precision,
                                                       vector_size, vec_arg_list,
                                                       vector_scheme, vector_mask,
                                                       vec_res, scalar_callback)

    else:
        function_scheme = generate_c_vector_wrapper(self.precision, vector_size,
                                                  vec_arg_list, vector_scheme,
                                                  vector_mask, vec_res,
                                                  scalar_callback)

    for vec_arg in vec_arg_list:
        self.implementation.register_new_input_variable(vec_arg)
    self.implementation.set_output_format(vector_output_format)

    # dummy scheme to make functionnal code generation
    self.implementation.set_scheme(function_scheme)

    Log.report(Log.Info, "[SV] end of generate_vector_implementation")
    return FunctionGroup([self.implementation], [scalar_callback_function])

def generate_c_vector_wrapper(vector_size, vec_arg_list,
                              vector_scheme, vector_mask, vec_res,
                              scalar_callback):
    """ Generate a C-compatible wrapper for a vectorized scheme 
        @p vector_scheme by testing vector mask element and branching
        to scalar callback when necessary
        
        @param vector_size number of element in a vector
        @param vector_arg_list
        @param vector_scheme
        @param vector_mask 
        @param vec_res Variable node destination of the scheme result
        @param scalar_callback Scalar function which implement the scalar version
                                of the vector scheme and which is used to manage
                                special cases """
    vi = Variable("i", precision = ML_Int32, var_type = Variable.Local)
    vec_elt_arg_tuple = tuple(
        VectorElementSelection(vec_arg, vi, precision=vec_arg.get_precision().get_scalar_format())
        for vec_arg in vec_arg_list
    )

    function_scheme = Statement(
        # prospective execution of the full vector scheme 
        vector_scheme,
        ConditionBlock(
            # if there is not a single zero in the mask. then
            # the vector result may be returned
            Test(
                vector_mask,
                specifier=Test.IsMaskNotAnyZero,
                precision=ML_Bool,
                likely=True,
                debug=debug_multi,
                tag="full_vector_valid"
            ),
            Return(vector_scheme, precision=vector_scheme.get_precision()),
            # else the vector must be processed and every element which
            # failed the validity test is dereffered to the scalar callback
            Statement(
                ReferenceAssign(vec_res, vector_scheme),
                Loop(
                    ReferenceAssign(vi, Constant(0, precision = ML_Int32)),
                    vi < Constant(vector_size, precision = ML_Int32),
                    Statement(
                        ConditionBlock(
                            LogicalNot(
                                Likely(
                                    VectorElementSelection(
                                        vector_mask, vi, precision = ML_Bool,
                                        tag="vmask_i",
                                    ),
                                    None
                                ),
                                precision = ML_Bool
                            ),
                            ReferenceAssign(
                                VectorElementSelection(
                                    vec_res, vi, precision=vec_res.get_precision().get_scalar_format(),
                                    tag="vres_i"
                                ),
                                scalar_callback(*vec_elt_arg_tuple)
                            )
                        ),
                        ReferenceAssign(vi, vi + 1)
                    ),
                ),
                Return(vec_res, precision=vec_res.get_precision())
            )
        )
    )
    return function_scheme

def generate_opencl_vector_wrapper(main_precision, vector_size, vec_arg_list,
                                   vector_scheme, vector_mask, vec_res,
                                   scalar_callback):
    """ Generate an OpenCL-compatible wrapper for a vectorized scheme 
        @p vector_scheme by testing vector mask element and branching
        to scalar callback when necessary """
    unrolled_cond_allocation = Statement()
    for i in range(vector_size):
        elt_index = Constant(i)
        vec_elt_arg_tuple = tuple(VectorElementSelection(vec_arg, elt_index, precision = main_precision) for vec_arg in vec_arg_list)
        unrolled_cond_allocation.add(
            ConditionBlock(
                Likely(
                    LogicalNot(
                        VectorElementSelection(
                            vector_mask, 
                            elt_index, 
                            precision = ML_Bool
                        ),
                        precision = ML_Bool
                    ),
                    None
                ),
                ReferenceAssign(VectorElementSelection(vec_res, elt_index, precision = main_precision), scalar_callback(*vec_elt_arg_tuple)),
            )
        ) 

    function_scheme = Statement(
        vector_scheme,
        ConditionBlock(
            Test(vector_mask, specifier = Test.IsMaskNotAnyZero, precision = ML_Bool, likely = True, debug = debug_multi),
            Return(vector_scheme),
            Statement(
                ReferenceAssign(vec_res, vector_scheme),
                unrolled_cond_allocation,
                Return(vec_res)
            )
        )
    )
    return function_scheme


## Base class for all metalibm function (metafunction)
class ML_FunctionBasis(object):
  name = "function_basis"
  arity = 1

  ## constructor
  #   @param all arguments are transmittaed throughs @p arguments object which
  #          should inherit from DefaultArgTemplate
  def __init__(self, args=DefaultArgTemplate):
    # selecting argument values among defaults
    self.display_after_opt = args.display_after_opt

    # enable/disable check_processor_support pass run
    self.check_processor_support = args.check_processor_support

    # NOTES: by default arity should not been defined from arguments
    #        it should be a property of the function class
    # self.arity = args.arity
    self.precision = args.precision
    # io_precisions must be:
    #     -> a list
    # XOR -> None to select [se;f.precision] * self.arity
    # NOTES: the default value for input_precisions is delayed to account for the case
    #        when arity is defined after function init
    self._input_precisions = args.input_precisions
    # self.input_precisions = [self.precision] * self.arity if args.input_precisions is None else args.input_precisions

    # enable the generation of numeric/functionnal auto-test
    self.auto_test_enable = (args.auto_test != False or args.auto_test_std != False or args.value_test != [])
    self.auto_test_number = args.auto_test
    self.auto_test_range = args.auto_test_range
    self.auto_test_std   = args.auto_test_std 
    self.value_test = args.value_test

    # enable the computation of maximal error during functional testing
    self.compute_max_error = args.compute_max_error
    self.break_error = args.break_error

    # enable and configure the generation of a performance bench
    self.bench_enabled = args.bench_test_number
    self.bench_test_number = args.bench_test_number
    self.bench_test_range = args.bench_test_range
    # number of benchmark loop to run
    self.bench_loop_num = args.bench_loop_num

    self.display_stdout = args.display_stdout

    # source building
    self.build_enable = args.build_enable
    # embedded binary (test function is linked as shared object and imported
    # into python environement)
    self.embedded_binary = args.embedded_binary
    self.force_cross_platform = args.cross_platform
    # binary execution
    self.execute_trigger = args.execute_trigger

    # Naming logic, using provided information if available, otherwise deriving from base_name
    # base_name is e.g. exp
    # function_name is e.g. expf or expd or whatever 
    self.function_name = args.function_name if args.function_name else libc_naming(args.base_name, [self.precision] + self.input_precisions)

    self.output_file = args.output_file if args.output_file else self.function_name + ".c"

    self.language = args.language
    self.debug_flag = args.debug
    self.decorate_code = args.decorate_code

    self.vector_size = args.vector_size
    self.sub_vector_size = args.sub_vector_size

    # TODO: FIX which i/o precision to select
    # TODO: incompatible with fixed-point formats
    # self.sollya_precision = self.get_output_precision().get_sollya_object()

    # self.abs_accuracy = args.abs_accuracy if args.abs_accuracy else S2**(-self.get_output_precision().get_precision())
    self.libm_compliant = args.libm_compliant
    self.accuracy_class = args.accuracy
    self.accuracy = args.accuracy(self.get_output_precision())

    self.input_intervals = args.input_intervals
    
    self.processor = args.target
    self.target_exec_options = args.target_exec_options

    self.fuse_fma = args.fuse_fma
    self.dot_product_enabled = args.dot_product_enabled
    self.fast_path_extract = args.fast_path_extract

    # plotting options
    self.plot_function = args.plot_function
    self.plot_error = args.plot_error
    self.plot_range = args.plot_range
    self.plot_steps = args.plot_steps
    # internal flag which indicate that at least one plot must be generated
    self.plot_enabled = self.plot_function or self.plot_error

    # instance of CodeFunction containing the function implementation
    self.implementation = CodeFunction(self.function_name, output_format=self.get_output_precision())
    # instance of OptimizationEngine
    self.opt_engine = OptimizationEngine(self.processor, dot_product_enabled=self.dot_product_enabled)
    # instance of GappaCodeGenerator to perform inline proofs
    self.gappa_engine = GappaCodeGenerator(self.processor, declare_cst=True, disable_debug=True)
    # instance of Code Generation to generate source code
    CODE_GENERATOR_CLASS = self.get_codegen_class(self.language)
    CODE_OBJECT_CLASS = self.get_codeobject_ctor(self.language)

    self.main_code_generator = CODE_GENERATOR_CLASS(
        self.processor, declare_cst=False, disable_debug=not self.debug_flag,
        libm_compliant=self.libm_compliant, language=self.language,
        decorate_code=self.decorate_code
    )
    uniquifier = self.function_name
    shared_symbol_list = [
        MultiSymbolTable.ConstantSymbol,
        MultiSymbolTable.TableSymbol,
        MultiSymbolTable.FunctionSymbol,
        MultiSymbolTable.EntitySymbol
    ] 
    if self.language is LLVM_IR_Code:
        shared_symbol_list.append(MultiSymbolTable.VariableSymbol)
        shared_symbol_list.append(MultiSymbolTable.LabelSymbol)
    # main code object
    self.main_code_object = NestedCode(
        self.main_code_generator, static_cst=True,
        uniquifier="{0}_".format(self.function_name),
        code_ctor=CODE_OBJECT_CLASS,
        shared_symbol_list=shared_symbol_list)

    # pass scheduler
    # pass scheduler instanciation
    self.pass_scheduler = PassScheduler(
        pass_tag_list=[
            PassScheduler.Start,
            PassScheduler.Typing,
            PassScheduler.Optimization,
            PassScheduler.JustBeforeCodeGen
        ]
    )

    self.processor.instanciate_pass_pipeline(self.pass_scheduler,
                                             self.processor,
                                             args.passes + args.extra_passes,
                                             language=self.language)

    Log.report(Log.LogLevel("DumpPassInfo"), self.pass_scheduler.dump_pass_info())

  @property
  def input_precisions(self):
    if self._input_precisions is None:
        self._input_precisions = [self.precision] * self.arity
    return self._input_precisions

  def get_codegen_class(self, language):
    """ return the code generator class associated with a given language """
    return {
        C_Code: CCodeGenerator,
        OpenCL_Code: CCodeGenerator,
        LLVM_IR_Code: LLVMIRCodeGenerator
    }[language]

  def get_codeobject_ctor(self, language):
    """ return the basic code object class associated with a given language """
    return {
        C_Code: CodeObject,
        OpenCL_Code: CodeObject,
        LLVM_IR_Code: LLVMCodeObject,
    }[language]

  def get_vector_size(self):
    return self.vector_size

  def get_execute_handle(self):
    """ return the name of the main function to be called on execution """
    Log.report(Log.Error, "current function has not execution handle (possible reason could be no main or no test/bench requested)", error=NotImplementedError)
    raise NotImplementedError


  ## generate a default argument template
  #  may be overloaded by sub-class to provide
  #  a meta-function specific default argument structure
  @staticmethod
  def get_default_args(**args):
    return DefaultArgTemplate(**args)

  ## compute the evaluation error of an ML_Operation node
  #  @param optree ML_Operation object whose evaluation error is computed
  #  @param variable_copy_map dict(optree -> optree) used to delimit the
  #         bound of optree
  #  @param goal_precision ML_Format object, precision used for evaluation goal
  #  @param gappa_filename string, name of the file where the gappa proof
  #         of the evaluation error will be dumped
  #  @return numerical value of the evaluation error
  def get_eval_error(
        self, optree, variable_copy_map = {}, goal_precision = ML_Exact,
        gappa_filename = "gappa_eval_error.g", relative_error = False
    ):
    """ wrapper for GappaCodeGenerator get_eval_error_v2 function """
    copy_map = {}
    for leaf in variable_copy_map:
      copy_map[leaf] = variable_copy_map[leaf]
    opt_optree = self.optimise_scheme(optree, copy = copy_map, verbose = False)
    new_variable_copy_map = {}
    for leaf in variable_copy_map:
      new_variable_copy_map[leaf.get_handle().get_node()] = variable_copy_map[leaf]
    return self.gappa_engine.get_eval_error_v2(
        self.opt_engine, opt_optree,
        new_variable_copy_map if variable_copy_map != None else {},
        goal_precision, gappa_filename, relative_error = relative_error
    )

  ## name generation
  #  @param base_name string, name to be extended for unifiquation
  def uniquify_name(self, base_name):
    """ return a unique identifier, combining base_name + function_name """
    return "%s_%s" % (self.function_name, base_name)

  ## emulation code generation
  def generate_emulate(self):
    raise Exception("generate emulate is deprecated")

  ## generation the wrapper to the emulation code
  #  @param test_input Variable where the test input is read from
  #  @param mpfr_rnd Variable object used as precision paramater for mpfr calls
  #  @param test_output Variable where emulation result is copied to
  #  @param test_ternary Variable where mpfr ternary status is copied to
  #  @return tuple code_object, code_generator 
  def generate_emulate_wrapper(self, test_input   = Variable("vx", precision = ML_Mpfr_t), mpfr_rnd = Variable("rnd", precision = ML_Int32), test_output = Variable("result", precision = ML_Mpfr_t, var_type = Variable.Local), test_ternary = Variable("ternary", precision = ML_Int32, var_type = Variable.Local)):
    scheme = self.generate_emulate(test_ternary, test_output, test_input, mpfr_rnd)

    wrapper_processor = MPFRProcessor()

    code_generator = CCodeGenerator(wrapper_processor, declare_cst = False, disable_debug = True, libm_compliant = self.libm_compliant)
    code_object = NestedCode(code_generator, static_cst = True)
    code_generator.generate_expr(code_object, scheme, folded = False, initial = False)
    return code_object, code_generator

  def get_output_precision(self):
    return self.precision
  def get_input_precision(self, index = 0):
    return self.input_precisions[index]
  def get_input_precisions(self):
    return self.input_precisions

  def get_sollya_precision(self):
    """ return the main precision use for sollya calls """
    return self.sollya_precision

  def generate_scheme(self):
    """ generate MDL scheme for function implementation """
    Log.report(Log.Error,
               "generate_scheme must be overloaded by ML_FunctionBasis child",
               error=NotImplementedError)

  ## Return the list of CodeFunction objects (main function
  #  and sub-functions) to be used to build @p self implementation
  #  This function may be overloaded by child class to define
  #  a specific way to build CodeFunction objects
  #
  # @return main_scheme, [list of sub-CodeFunction object]
  def generate_function_list(self):
    self.implementation.set_scheme(self.generate_scheme())
    return FunctionGroup([self.implementation])

  ## submit operation node to a standard optimization procedure
  #  @param pre_scheme ML_Operation object to be optimized
  #  @param copy  dict(optree -> optree) copy map to be used while duplicating
  #               pre_scheme (if None disable copy)
  #  @param enable_subexpr_sharing boolean flag, enables sub-expression sharing
  #         optimization
  #  @param verbose boolean flag, enable verbose mode
  #  @return optimizated scheme
  def optimise_scheme(self, pre_scheme, copy = None,
                      enable_subexpr_sharing = True, verbose = True):
    """ default scheme optimization """
    # copying when required
    scheme = pre_scheme if copy is None else pre_scheme.copy(copy)
    # fusing FMA
    if self.fuse_fma:
      Log.report(Log.Verbose, "MDL fusing FMA")
      scheme = self.opt_engine.fuse_multiply_add(scheme, silence = True)

    Log.report(Log.Verbose, "MDL abstract scheme")
    self.opt_engine.instantiate_abstract_precision(scheme,
                                                   default_precision = None)

    Log.report(Log.Verbose, "MDL instantiated scheme")
    self.opt_engine.instantiate_precision(scheme, default_precision = None)

    if enable_subexpr_sharing:
      Log.report(Log.Verbose, "subexpression sharing")
      self.opt_engine.subexpression_sharing(scheme)

    Log.report(Log.Verbose, "silencing operation")
    self.opt_engine.silence_fp_operations(scheme)

    return scheme


  ##
  #  @return main code object associted with function implementation
  def get_main_code_object(self):
    return self.main_code_object


  def generate_code(self, function_group, language):
    if self.language == C_Code:
        return self.generate_C_code(function_group, language=language)
    elif self.language == LLVM_IR_Code:
        return self.generate_LLVM_code(function_group, language=language)

  def generate_LLVM_code(self, function_group, language = LLVM_IR_Code):
    """ Final LLVM-IR generation, once the evaluation scheme has been optimized"""
    Log.report(Log.Info, "Generating Source Code ")
    # main code object
    code_object = self.get_main_code_object()
    self.result = code_object

    def gen_code_function_code(fct_group, fct):
        self.result = fct.add_definition(
            self.main_code_generator,
            language, code_object, static_cst=True)

    function_group.apply_to_all_functions(gen_code_function_code)

    #for code_function in code_function_list:
    #  self.result = code_function.add_definition(self.main_code_generator,
    #                                             language, code_object,
    #                                             static_cst = True)

    return self.result

  ## generate C code for function implenetation
  #  Code is generated within the main code object
  #  and dumped to a file named after implementation's name
  #  @param code_function_list list of CodeFunction to be generated (as sub-function )
  #  @return void
  def generate_C_code(self, function_group, language = C_Code):
    """ Final C generation, once the evaluation scheme has been optimized"""
    Log.report(Log.Info, "Generating Source Code ")
    # main code object
    code_object = self.get_main_code_object()
    self.result = code_object

    def gen_code_function_decl(fct_group, fct):
        self.result = fct.add_declaration(
            self.main_code_generator, language, code_object
        )

    def gen_code_function_code(fct_group, fct):
        self.result = fct.add_definition(
            self.main_code_generator,
            language, code_object, static_cst=True)

    # generating function declaration
    function_group.apply_to_all_functions(gen_code_function_decl)
    # generation function definition
    function_group.apply_to_all_functions(gen_code_function_code)

    #for code_function in code_function_list:
    #  self.result = code_function.add_definition(self.main_code_generator,
    #                                             language, code_object,
    #                                             static_cst = True)

    # adding headers
    self.result.add_header("support_lib/ml_special_values.h")
    self.result.add_header("math.h")
    if self.debug_flag: self.result.add_header("stdio.h")
    self.result.add_header("inttypes.h")

    return self.result


  def fill_code_object(self, enable_subexpr_sharing=True):
    """ generate source code as CodeObject

        Args:
            enable_subexpr_sharing (bool): I.R enable sub-expression sharing
               optimization

        """
    # generate scheme
    function_group = self.generate_function_list()

    function_group = self.transform_function_group(function_group)

    main_pre_statement, main_statement, function_group = self.instrument_function_group(function_group, enable_subexpr_sharing=enable_subexpr_sharing)

    embedding_binary = self.embedded_binary and self.processor.support_embedded_bin

    source_code = self.generate_output(embedding_binary, main_pre_statement, main_statement, function_group)
    return function_group, source_code

  def generate_full_source_code(self, enable_subexpr_sharing=True):
    _, source_code_object = self.fill_code_object(enable_subexpr_sharing=enable_subexpr_sharing)
    return source_code_object.get(self.main_code_generator)


  def gen_implementation(self, display_after_gen=False,
                         display_after_opt=False,
                         enable_subexpr_sharing=True):
    """ Generate source code in self.output_file and execute result if any

        Args:
            display_after_gen enable (bool): I.R dump after generation
            display_after_opt enable (bool): I.R dump after optimization
            enable_subexpr_sharing (bool): I.R enable sub-expression sharing
    """

    function_group, source_code = self.fill_code_object(enable_subexpr_sharing=enable_subexpr_sharing)

    Log.report(Log.Info, "Generating C code in " + self.output_file)
    with open(self.output_file, "w") as output_stream:
        output_stream.write(source_code.get(self.main_code_generator))
    source_file = SourceFile(self.output_file, function_group)
    # returning execution result, if any
    embedding_binary = self.embedded_binary and self.processor.support_embedded_bin
    execute_result = self.execute_output(embedding_binary, source_file)
    print("execute_result: {}".format(execute_result))
    return execute_result


  def transform_function_group(self, function_group):
    """ Apply registered passes to a function_group """
    Log.report(Log.Info, "Applying <Start> stage passes")
    _ = self.pass_scheduler.get_full_execute_from_slot(
      function_group,
      PassScheduler.Start,
      execute_pass_on_fct_group
    )

    # generate vector size
    if self.get_vector_size() != 1:
        scalar_scheme = self.implementation.get_scheme()
        # extract implementation's argument list
        scalar_arg_list = self.implementation.get_arg_list()
        # clear argument list (will be replaced by vectorized counterpart)
        self.implementation.clear_arg_list()

        function_group = self.generate_vector_implementation(
            scalar_scheme, scalar_arg_list, self.get_vector_size()
        )

    # format instantiation
    Log.report(Log.Info, "Applying <Typing> stage passes")
    _ = self.pass_scheduler.get_full_execute_from_slot(
        function_group,
        PassScheduler.Typing,
        execute_pass_on_fct_group
    )

    # format instantiation
    Log.report(Log.Info, "Applying <Optimization> stage passes")
    _ = self.pass_scheduler.get_full_execute_from_slot(
        function_group,
        PassScheduler.Optimization,
        execute_pass_on_fct_group
    )

    # format instantiation
    Log.report(Log.Info, "Applying <JustBeforeCodeGen> stage passes")
    _ = self.pass_scheduler.get_full_execute_from_slot(
        function_group,
        PassScheduler.JustBeforeCodeGen,
        execute_pass_on_fct_group
    )
    return function_group


  def instrument_function_group(self, function_group, enable_subexpr_sharing=False):
    """ Insert testing and benchmarking tooling into function_group
    
        :param function_group: group of function to be instrumented
        :type function_group: FunctionGroup
        :param enable_subexpr_sharing: enable sharing of sub-expression during op graph optimization
        :type enable_subexpr_sharing: bool
        :return: resulting instrumented group of function
        :rtype: FunctionGroup """
    main_pre_statement = Statement()
    main_statement = Statement()

    CstError = Constant(1, precision=ML_Int32)

    def add_fct_call_check_in_main(fct_group, code_function, check=lambda op: op):
        """ adding call to code_function with return value check
            in main statement """
        scheme = code_function.get_scheme()
        opt_scheme = self.optimise_scheme(
            scheme, enable_subexpr_sharing = enable_subexpr_sharing
        )
        code_function.set_scheme(opt_scheme)
        fct_call = code_function.build_function_object()()
        main_pre_statement.add(fct_call)
        main_statement.add(
            ConditionBlock(
                check(fct_call),
                Return(CstError)
            )
        )

    # generate auto-test wrapper
    if self.auto_test_enable or self.compute_max_error:
        # common test tables for auto_test and max_error
        test_num = self.auto_test_number if self.auto_test_number else 0
        DEFAULT_MAX_ERROR_TEST_NUMBER = 10000

        if not self.auto_test_enable and self.compute_max_error:
            test_num = DEFAULT_MAX_ERROR_TEST_NUMBER
        test_total, input_tables, output_table = self.generate_test_tables(
                test_num=test_num,
                test_ranges = self.auto_test_range)

        if self.auto_test_enable:
            auto_test_function_group = self.generate_test_wrapper(
                test_total, input_tables, output_table
            )
            auto_test_function_group.apply_to_all_functions(add_fct_call_check_in_main)
            # appending auto-test wrapper to general code_function_list
            function_group.merge_with_group(auto_test_function_group)

        # generate max-error test wrapper
        if self.compute_max_error:
            # arguments
            tested_function    = self.implementation.get_function_object()

            max_error_function = CodeFunction("max_error_wrapper", output_format=self.precision) 
            max_error_main_statement = self.generate_scalar_max_error_wrapper(tested_function, test_total, input_tables, output_table) 
            max_error_function.set_scheme(max_error_main_statement)
            max_error_fct_group = FunctionGroup([max_error_function])
            max_error_fct_group.apply_to_all_functions(add_fct_call_check_in_main)
            function_group.merge_with_group(max_error_fct_group)

    if self.bench_enabled:
        bench_function_group = self.generate_bench_wrapper(
            test_num=self.bench_test_number if self.bench_test_number else 1000,
            loop_num=self.bench_loop_num,
            test_ranges=self.bench_test_range
        )

        def bench_check(bench_call):
            return Comparison(bench_call, Constant(0.0, precision=ML_Binary64), specifier=Comparison.Less, precision=ML_Bool)

        bench_function_group.apply_to_all_functions(
            lambda fct_group, cf: add_fct_call_check_in_main(fct_group, cf, bench_check)
        )
        # appending bench wrapper to general code_function_list
        function_group.merge_with_group(bench_function_group)
    return main_pre_statement, main_statement, function_group


  def generate_output(self, embedding_binary, main_pre_statement, main_statement, function_group):
    """ Generate output of code generation (including source file if any)
        :param embedding_binary: enable embedded of binary executable file in
                                 python program (allowing easier result extraction)
        :type embedding_binary: bool
        :param main_pre_statement: Statement node before main section
        :type main_pre_statement: Statement
        :param main_statement: Statement node for main section
        :type main_statement: Statement
        :param function_group: group of function being generated
        :type function_group: FunctionGroup
        :return: source file result
        :rtype: SourceFile

    """
    CstSuccess = Constant(0, precision=ML_Int32)
    # adding main function
    if not embedding_binary:
        main_function = CodeFunction("main", output_format=ML_Int32)
        main_function.set_scheme(
            Statement(
                main_pre_statement,
                main_statement,
                Return(CstSuccess)
            )
        )
        function_group.add_core_function(main_function)

    # generate C code to implement scheme
    source_file = self.generate_code(function_group, language=self.language)
    return source_file


  def execute_output(self, embedding_binary, source_file):
    """ If any, run executable code from source file and extract results
        :param embedding_binary: enable embedded of binary executable file in
                                 python program (allowing easier result extraction)
        :type embedding_binary: bool
        :param source_file: source file containing code to be executed
        :type source_file: SourceFile

    """
    # plotting function requires it to be build and imported
    build_trigger = self.build_enable or self.execute_trigger or self.plot_enabled
    link_trigger = self.execute_trigger

    exec_result = {}

    if build_trigger:
        if embedding_binary:
            bin_name = "./testlib_%s.so" % self.function_name
            shared_object = True
            link_trigger = False
        else:
            bin_name = "./testbin_%s" % self.function_name
            shared_object = False
            link_trigger = True
        bin_file = source_file.build(self.processor, bin_name, shared_object=shared_object, link=link_trigger)

        if bin_file is None:
            Log.report(Log.Error, "build failed: \n", error=BuildError())
        else:
            Log.report(Log.Info, "build succedeed\n")

        # only plotting if build was successful
        if self.plot_enabled:
            if not embedding_binary:
                Log.report(Log.Error, "plot only work with embedded binary (--no-embedded-bin not supported)")
            if matplotlib is None:
                Log.report(Log.Error, "matplotlib module is not availble (required for plot)")
            loaded_module = bin_file.loaded_binary
            plot_range_size = sup(self.plot_range) - inf(self.plot_range)
            x_list = [float(inf(self.plot_range) + i / self.plot_steps * plot_range_size) for i in range(self.plot_steps)] 
            # extracting main function from compiled binary
            binary_function = loaded_module.get_function_handle(self.function_name)
            if self.plot_function:
                matplotlib.plot(x_list, [binary_function(v) for v in x_list])
                matplotlib.ylabel('{}(x) function plot'.format(self.function_name))
            if self.plot_error:
                error_list = [(abs(sollya.round((binary_function(v) - self.numeric_emulate(v)) / self.numeric_emulate(v), sollya.binary64, sollya.RN))) for v in x_list]
                error_list = [float(v) for v in error_list]
                matplotlib.plot(x_list, error_list, 'bo')
                matplotlib.yscale('log', basey=2) 
                matplotlib.ylabel('{}(x) error plot'.format(self.function_name))
            matplotlib.show()

        # only executing if build was successful
        if self.execute_trigger:
            if embedding_binary and not(bin_file is None):
                loaded_module = bin_file.loaded_binary

                if self.bench_enabled:
                    cpe_measure = loaded_module.get_function_handle("bench_wrapper")()
                    exec_result["cpe_measure"] = cpe_measure

                if self.auto_test_enable:
                    test_result = loaded_module.get_function_handle("test_wrapper")()
                    exec_result["test_result"] = test_result
                    if not test_result:
                        Log.report(Log.Info, "VALIDATION SUCCESS")
                    else:
                        Log.report(Log.Error, "VALIDATION FAILURE", error=ValidError())

                if self.compute_max_error:
                    max_error_value = loaded_module.get_function_handle("max_error_wrapper")()
                    exec_result["max_error"] = max_error_value

                # if no specific measure/test is schedule we execute the function itself
                if not (self.bench_enabled or self.auto_test_enable or self.compute_max_error):
                    execution_result = loaded_module.get_function_handle(self.get_execute_handle())()
                return exec_result

            elif not embedding_binary:
                if self.force_cross_platform or self.processor.cross_platform:
                    if self.target_exec_options is None:
                        execute_cmd = self.processor.get_execution_command(bin_file.path)
                    else:
                        execute_cmd = self.processor.get_execution_command(bin_file.path, **self.target_exec_options)
                    Log.report(Log.Info, "execute cmd: {}", execute_cmd)
                    test_result, ret_stdout = get_cmd_stdout(execute_cmd)
                    ret_stdout = str(ret_stdout)
                    # extracting benchmark result
                    if self.bench_enabled:
                        cpe_match = re.search("(?P<cpe_measure>\d+\.\d+) CPE", ret_stdout)
                        if cpe_match is None:
                            Log.report(Log.Error, "not able to extract CPE measure from log: {}", ret_stdout)
                        try:
                            cpe_measure = float(cpe_match.group("cpe_measure"))
                            exec_result["cpe_measure"] = cpe_measure
                        except Exception as e:
                            Log.report(Log.Error, "unable to extract float cpe measure from {}", cpe_match.group("cpe_measure"), error=e)
                    # extracting max error result
                    if self.compute_max_error:
                        max_error = re.search("max error is (?P<max_error>\d+\.\d+)", ret_stdout)
                        if max_error is None:
                            Log.report(Log.Error, "not able to extract max error measure from log: {}", ret_stdout)
                        try:
                            max_error = sollya.parse(max_error.group("max_error"))
                            exec_result["max_error"] = max_error
                        except Exception as e:
                            Log.report(Log.Error, "unable to extract sollya.parse max-error measure from {}", max_error.group("max_error"), error=e)
                else:
                    Log.report(Log.Info, "executing : {}", bin_file.path)
                    test_result, ret_stdout = bin_file.execute()
                Log.report(Log.Info, "log: {}", ret_stdout)
                if not test_result:
                    Log.report(Log.Info, "VALIDATION SUCCESS")
                else:
                    Log.report(Log.Error, "VALIDATION FAILURE", error=ValidError())
                return exec_result
            return None
        return None



  ## externalized an optree: generate a CodeFunction which compute the 
  #  given optree inside a sub-function and returns it as a result
  # @param optree ML_Operation object to be externalized
  # @param arg_list list of ML_Operation objects to be used as arguments
  # @return pair ML_Operation, CodeFunction
  def externalize_call(self, optree, arg_list, tag = "foo", result_format = None, name_factory = None):
    # Call externalizer engine
    ext_function = generate_function_from_optree(self.get_main_code_object(), optree, arg_list, tag, result_format)
    return ext_function.get_function_object()(*arg_list), ext_function


  def generate_vector_implementation(self, scalar_scheme, scalar_arg_list,
                                     vector_size=2):
    """ generate a vector implementation of self's scheme """
    # declaring optimizer
    self.opt_engine.set_boolean_format(ML_Bool)
    self.vectorizer = StaticVectorizer()

    callback_name = self.uniquify_name("scalar_callback")

    scalar_callback_function = generate_function_from_optree(self.get_main_code_object(), scalar_scheme, scalar_arg_list, callback_name, self.precision)
    # adding static attributes
    scalar_callback_function.add_attribute("static")

    Log.report(Log.Info, "[SV] optimizing Scalar scheme")
    scalar_scheme = self.optimise_scheme(scalar_scheme)
    scalar_scheme.set_tag("scalar_scheme")

    scalar_callback          = scalar_callback_function.get_function_object()

    # Call externalizer engine
    call_externalizer = CallExternalizer(self.get_main_code_object())

    Log.report(Log.Info, "[SV] vectorizing scheme")
    sub_vector_size = self.processor.get_preferred_sub_vector_size(self.precision, vector_size) if self.sub_vector_size is None else self.sub_vector_size
    vec_arg_list, vector_scheme, vector_mask = \
        self.vectorizer.vectorize_scheme(scalar_scheme, scalar_arg_list,
                                         vector_size, sub_vector_size)

    vector_output_format = self.vectorizer.vectorize_format(self.precision,
                                                            vector_size)


    Log.report(Log.Info, "vector_output_format is {}".format(vector_output_format))
    vec_res = Variable("vec_res", precision=vector_output_format,
                       var_type = Variable.Local)


    vector_mask.set_attributes(tag = "vector_mask", debug = debug_multi)

    if self.language in [C_Code, OpenCL_Code]:
        self.get_main_code_object().add_header("support_lib/ml_vector_format.h")

    Log.report(Log.Info, "[SV] building vectorized main statement")
    if no_scalar_fallback_required(vector_mask):
      function_scheme = Statement(
        Return(vector_scheme, precision=vector_output_format)
      )
    elif self.language is OpenCL_Code:
      function_scheme = generate_opencl_vector_wrapper(self.main_precision,
                                                       vector_size, vec_arg_list,
                                                       vector_scheme, vector_mask,
                                                       vec_res, scalar_callback)

    else:
      function_scheme = generate_c_vector_wrapper(vector_size,
                                                  vec_arg_list, vector_scheme,
                                                  vector_mask, vec_res,
                                                  scalar_callback)

    for vec_arg in vec_arg_list:
      self.implementation.register_new_input_variable(vec_arg)
    self.implementation.set_output_format(vector_output_format)

    # dummy scheme to make functionnal code generation
    self.implementation.set_scheme(function_scheme)

    Log.report(Log.Info, "[SV] end of generate_vector_implementation")
    return FunctionGroup([self.implementation], [scalar_callback_function])


  # Currently mostly empty, to be populated someday
  def gen_emulation_code(self, precode, code, postcode):
    """generate C code that emulates the function, typically using MPFR.
    precode is declaration code (before the test loop)
    postcode is clean-up code (after the test loop)
    Takes the input and output names from input_list and output_list.
    Must postfix output names with "ref_", "ref_ru_", "ref_rd_"

    This class method performs commonly used initializations. 
    It initializes the MPFR versions of the inputs and outputs, 
    with the same names prefixed with "mp" and possibly postfixed with "rd" and "ru".

    It should be overloaded by actual metafunctions, and called by the overloading function. 
    """

  ## provide numeric evaluation of the main function on @p input_value
  #  @param input_value SollyaObject numeric input value
  #  @return SollyaObject numeric output value corresponding
  #          to emulation of @p self function on @p input_value
  def numeric_emulate(self, input_value):
    raise NotImplementedError

  def generate_rand_input_iterator(self, test_num, test_ranges):
    """ generate a random list of test inputs """
    # TODO/FIXME: implement proper input range depending on input index
    rng_map = [get_precision_rng(precision, inf(test_range), sup(test_range)) for precision, test_range in zip(self.input_precisions, test_ranges)]

    # random test cases
    for i in range(test_num):
      input_list = []
      for in_id in range(self.arity):
        # this random generator is limited to python float precision
        # (generally machine double precision)
        # TODO/FIXME: implement proper high precision generation
        # based on real input_precision (e.g. ML_DoubleDouble)
        input_precision = self.get_input_precision(in_id)
        # input_value = rng_map[in_id].get_new_value() # random.uniform(low_input, high_input)
        low_input = inf(test_ranges[in_id])
        high_input = sup(test_ranges[in_id])
        input_value =  random.uniform(low_input, high_input)
        input_value = input_precision.round_sollya_object(input_value, sollya.RN)
        input_list.append(input_value)
      yield tuple(input_list)


  def generate_test_tables(self, test_num, test_ranges=[Interval(-1.0, 1.0)]):
    """ Generate inputs and output table to be shared between auto test
        and max_error tests """
    test_total   = test_num 
    non_random_test_cases = []
    # add them to the total if standard test enabled
    if self.auto_test_std:
        # compute the number of standard test cases
        num_std_case = len(self.standard_test_cases)
        if not num_std_case:
            Log.report(Log.Warning, "{} standard test case found!", num_std_case)
        non_random_test_cases += self.standard_test_cases
        test_total += num_std_case

    # add value specific tests
    if self.value_test != []:
        test_total += len(self.value_test)
        non_random_test_cases += self.value_test

    # round up the number of tests to the implementation vector-size
    diff = (self.get_vector_size() - (test_total % self.get_vector_size())) % self.get_vector_size()
    assert diff >= 0
    test_total += diff
    test_num   += diff

    Log.report(Log.Info, "test test_total, test_num, diff: {} {} {}".format(test_total, test_num, diff))
    input_tables = [
      ML_NewTable(
        dimensions = [test_total],
        storage_precision = self.get_input_precision(i),
        tag = self.uniquify_name("input_table_arg%d" % i)
      ) for i in range(self.arity)
    ]

    ## output values required to check results are stored in output table
    num_output_value = self.accuracy.get_num_output_value()
    output_table = ML_NewTable(dimensions=[test_total, num_output_value],
                               storage_precision=self.precision,
                               tag=self.uniquify_name("output_table"))

    test_case_list = []

    if len(non_random_test_cases):
      # standard test cases
      for i, test_case in enumerate(non_random_test_cases):
        input_list = []
        for in_id in range(self.arity):
          input_value = self.get_input_precision(in_id).round_sollya_object(test_case[in_id], sollya.RN)
          input_list.append(input_value)
        test_case_list.append(tuple(input_list))

    # adding randomly generated inputs
    test_case_list += list(self.generate_rand_input_iterator(test_num, test_ranges))

    # generating output from the concatenated list of all inputs
    for table_index, input_tuple in enumerate(test_case_list):
      # storing inputs
      for in_id in range(self.arity):
        input_tables[in_id][table_index] = input_tuple[in_id]
      # computing and storing output values
      output_values = self.accuracy.get_output_check_value(self.numeric_emulate(*input_tuple))
      for o in range(num_output_value):
        output_table[table_index][o] = output_values[o]

    return test_total, input_tables, output_table
  

  ## Generate a test wrapper for the @p self function 
  #  @param test_num   number of test to perform
  #  @param test_range numeric range for test's inputs
  #  @param debug enable debug mode
  def generate_test_wrapper(self, test_total, input_tables, output_table):
    auto_test = CodeFunction("test_wrapper", output_format = ML_Int32)

    tested_function    = self.implementation.get_function_object()
    function_name      = self.implementation.get_name()

    failure_report_op       = FunctionOperator("report_failure")
    failure_report_function = FunctionObject("report_failure", [], ML_Void, failure_report_op)

    printf_success_op = FunctionOperator("printf", arg_map = {0: "\"test successful %s\\n\"" % function_name}, void_function = True) 
    printf_success_function = FunctionObject("printf", [], ML_Void, printf_success_op)




    if self.implementation.get_output_format().is_vector_format():
      # vector implementation test
      test_loop = self.get_vector_test_wrapper(test_total, tested_function, input_tables, output_table)
    else: 
      # scalar implemetation test
      test_loop = self.get_scalar_test_wrapper(test_total, tested_function, input_tables, output_table)

    # common test scheme between scalar and vector functions
    test_scheme = Statement(
      test_loop,
      printf_success_function(),
      Return(Constant(0, precision = ML_Int32))
    )
    auto_test.set_scheme(test_scheme)

    return FunctionGroup([auto_test])

  ## return a FunctionObject display
  #  an error index, a list of argument values
  #  and a result value
  def get_printf_input_function(self):
    # build the complete format string from the input precisions
    input_display_formats = ", ".join(prec.get_display_format(self.language).format_string for prec in self.get_input_precisions())
    input_display_vars = ", ".join(prec.get_display_format(self.language).pre_process_fct("{%d}" % index) for index, prec in enumerate(self.get_input_precisions(), 1))

    result_arg_id = 1 + len(self.get_input_precisions())
    # expected_arg_id = 1 + result_arg_id
    # build the format string for result/expected display
    result_display_format = self.precision.get_display_format(self.language).format_string
    result_display_vars = self.precision.get_display_format(self.language).pre_process_fct("{%d}" % result_arg_id)
    # expected_display_vars = self.precision.get_display_format(self.language).pre_process_fct("{%d}" % expected_arg_id)

    template = ("printf(\"error[%d]: {fct_name}({arg_display_format}),"
                " result is {result_display_format} "
                "vs expected \""
                ", {{0}}, {arg_display_vars}, {result_display_vars}"
                ")").format(
                    fct_name=self.function_name,
                    arg_display_format=input_display_formats,
                    arg_display_vars=input_display_vars,
                    result_display_format=result_display_format,
                    #expected_display_format=result_display_format,
                    result_display_vars=result_display_vars,
                    #expected_display_vars=expected_display_vars
                )
    printf_op = TemplateOperatorFormat(template, void_function=True, arity=(result_arg_id+1), require_header=["stdio.h"]) 
    printf_input_function = FunctionObject("printf", [ML_Int32] + self.get_input_precisions() + [self.precision], ML_Void, printf_op)
    return printf_input_function

  ## generate a test loop for vector tests
  #  @param test_num number of elementary tests to be executed
  #  @param tested_function FunctionObject to be tested
  #  @param input_tables list of ML_NewTable object containing test inputs
  #  @param output_table ML_NewTable object containing test outputs
  def get_vector_test_wrapper(self, test_num, tested_function, input_tables, output_table):
    vector_format = self.implementation.get_output_format()
    assignation_statement = Statement()
    vi = Variable("i", precision = ML_Int32, var_type = Variable.Local)
    test_num_cst = Constant(test_num, precision = ML_Int32, tag = "test_num")

    # building inputs
    local_inputs = [
      Variable(
        "vec_x_{}".format(i) , 
        precision = vector_format, 
        var_type = Variable.Local
      ) for i in range(self.arity)
    ]
    for input_index, local_input in enumerate(local_inputs):
      assignation_statement.push(local_input)
      for k in range(self.get_vector_size()):
        for ref_assign in vector_elt_assign(local_input, k, TableLoad(input_tables[input_index], vi + k)):
            assignation_statement.push(ref_assign)

    # computing results
    local_result = tested_function(*local_inputs)
    loop_increment = self.get_vector_size()

    comp_statement = Statement()
    
    printf_input_function = self.get_printf_input_function()

    # comparison with expected
    for k in range(self.get_vector_size()):
      elt_inputs  = [VectorElementSelection(local_inputs[input_id], k) for input_id in range(self.arity)]
      elt_result = VectorElementSelection(local_result, k)

      output_values = [TableLoad(output_table, vi + k, i) for i in range(self.accuracy.get_num_output_value())]

      failure_test = self.accuracy.get_output_check_test(elt_result, output_values)

      comp_statement.push(
        ConditionBlock(
          failure_test,
          Statement(
            printf_input_function(*tuple([vi + k] + elt_inputs + [elt_result])), 
            self.accuracy.get_output_print_call(self.function_name, output_values),
            Return(Constant(1, precision = ML_Int32))
          )
        )
      )
    
    # common test Statement
    test_statement = Statement()

    test_loop = Loop(
      ReferenceAssign(vi, Constant(0, precision = ML_Int32)),
      vi < test_num_cst,
      Statement(
        assignation_statement,
        comp_statement,
        ReferenceAssign(vi, vi + loop_increment)
      ),
    )

    # computing maximal error
    if self.compute_max_error:
      eval_error = Variable("max_error", precision = self.precision, var_type = Variable.Local)

      printf_error_template = "printf(\"%s's max error is %s \\n\", %s)" % (
        self.function_name,
        self.precision.get_display_format(self.language).format_string,
        self.precision.get_display_format(self.language).pre_process_fct("{0}")
      )
      # printf_error_op = FunctionOperator("printf", arg_map = {0: "\"max %s error is %s \\n \"" % (self.function_name, self.precision.get_display_format(self.language)), 1: FO_Arg(0)}, void_function = True) 
      printf_error_op = TemplateOperatorFormat(printf_error_template, arity=1, void_function=True)
      printf_error_function = FunctionObject("printf", [self.precision], ML_Void, printf_error_op)

      local_inputs = [
        Variable(
          "vec_x_{}".format(i) ,
          precision = vector_format,
          var_type = Variable.Local
        ) for i in range(self.arity)
      ]
      assignation_statement = Statement()
      for input_index, local_input in enumerate(local_inputs):
        assignation_statement.push(local_input)
        for k in range(self.get_vector_size()):
          elt_assign = ReferenceAssign(VectorElementSelection(local_input, k), TableLoad(input_tables[input_index], vi + k))
          assignation_statement.push(elt_assign)

      # computing results
      local_result = tested_function(*local_inputs)

      comp_statement = Statement()
      for k in range(self.get_vector_size()):
        elt_inputs = [VectorElementSelection(local_inputs[input_id], k) for input_id in range(self.arity)]
        elt_result = VectorElementSelection(local_result, Constant(k, precision = ML_Integer))

        output_values = [TableLoad(output_table, vi + k, i) for i in range(self.accuracy.get_num_output_value())]

        local_error = self.accuracy.compute_error(elt_result, output_values, relative = True)

        comp_statement.push(
          ReferenceAssign(
            eval_error,
            Max(
              local_error,
              eval_error,
              precision = self.precision
            )
          )
        )

      error_loop = Loop(
        ReferenceAssign(vi, Constant(0, precision = ML_Int32)),
        vi < test_num_cst,
        Statement(
          assignation_statement,
          comp_statement,
          ReferenceAssign(vi, vi + loop_increment)
        ),
      )
      test_statement.add(
        Statement(
          ReferenceAssign(eval_error, Constant(0, precision = self.precision)),
          error_loop,
          printf_error_function(eval_error)
        )
      )

    # adding functional test_loop to test statement
    test_statement.add(test_loop)
    return test_statement

  ## generate a test loop for scalar tests
  #  @param test_num number of elementary tests to be executed
  #  @param tested_function FunctionObject to be tested
  #  @param input_table ML_NewTable object containing test inputs
  #  @param output_table ML_NewTable object containing test outputs
  #  @param printf_function FunctionObject to print error case
  def get_scalar_test_wrapper(self, test_num, tested_function, input_tables, output_table):
    assignation_statement = Statement()
    # loop iterator
    vi = Variable("i", precision = ML_Int32, var_type = Variable.Local)
    test_num_cst = Constant(test_num, precision = ML_Int32, tag = "test_num")


    local_inputs  = tuple(TableLoad(input_tables[in_id], vi) for in_id in range(self.arity))
    local_result = tested_function(*local_inputs)
    output_values = [TableLoad(output_table, vi, i) for i in range(self.accuracy.get_num_output_value())]

    failure_test = self.accuracy.get_output_check_test(local_result, output_values)

    printf_input_function = self.get_printf_input_function()

    printf_error_template = "printf(\"max %s error is %s \\n\", %s)" % (
      self.function_name,
      self.precision.get_display_format(self.language).format_string,
      self.precision.get_display_format(self.language).pre_process_fct("{0}")
    )
    printf_error_op = TemplateOperatorFormat(printf_error_template, arity=1, void_function=True)

    printf_error_function = FunctionObject("printf", [self.precision], ML_Void, printf_error_op)
    

    loop_increment = self.get_vector_size()
    
    if self.break_error:
        return_statement_break = Statement(
            printf_input_function(*((vi,) + local_inputs + (local_result,))), 
            self.accuracy.get_output_print_call(self.function_name, output_values)
        )
    else:
        return_statement_break = Statement(
            printf_input_function(*((vi,) + local_inputs + (local_result,))), 
            self.accuracy.get_output_print_call(self.function_name, output_values),
            Return(Constant(1, precision = ML_Int32))
        )
    
    test_loop = Loop(
      ReferenceAssign(vi, Constant(0, precision = ML_Int32)),
      vi < test_num_cst,
      Statement(
        assignation_statement,
        ConditionBlock(
          failure_test,
          return_statement_break,
        ),
        ReferenceAssign(vi, vi + loop_increment)
      ),
    )

    test_statement = Statement() 


    # adding functional test_loop to test statement
    test_statement.add(test_loop)

    return test_statement
  def generate_scalar_max_error_wrapper(self, tested_function, test_num, input_tables, output_table):
      # loop iterator
      vi = Variable("i", precision=ML_Int32, var_type=Variable.Local)

      max_error_relative = Variable("max_error_relative", precision=self.precision, var_type=Variable.Local)
      max_error_absolute = Variable("max_error_absolute", precision=self.precision, var_type=Variable.Local)

      max_input = Variable("max_input", precision = ML_Int32, var_type = Variable.Local)
      max_result = Variable("max_result", precision = self.precision, var_type = Variable.Local)
      max_vi = Variable("max_vi", precision = ML_Int32, var_type = Variable.Local)
      local_inputs  = tuple(TableLoad(input_tables[in_id], vi) for in_id in range(self.arity))
      test_num_cst = Constant(test_num, precision=ML_Int32, tag="test_num")

      local_result  = tested_function(*local_inputs)
      stored_values = [TableLoad(output_table, vi, i) for i in range(self.accuracy.get_num_output_value())]
      local_error_relative = self.accuracy.compute_error(local_result, stored_values, relative=True)
      local_error_absolute = self.accuracy.compute_error(local_result, stored_values, relative=False)

      error_rel_comp = Comparison(local_error_relative, max_error_relative, specifier=Comparison.Greater, precision=ML_Bool)
      error_abs_comp = Comparison(local_error_absolute, max_error_absolute, specifier=Comparison.Greater, precision=ML_Bool)

      loop_increment = 1
      printf_error_template = "printf(\"max %s error is absolute=%s, relative=%s \\n\", %s, %s)" % (
        self.function_name,
        self.precision.get_display_format(self.language).format_string,
        self.precision.get_display_format(self.language).format_string,
        self.precision.get_display_format(self.language).pre_process_fct("{0}"),
        self.precision.get_display_format(self.language).pre_process_fct("{0}")
      )
      printf_error_op = TemplateOperatorFormat(printf_error_template, arity=2, void_function=True, require_header=["stdio.h"])

      printf_error_function = FunctionObject("printf", [self.precision, self.precision], ML_Void, printf_error_op)
      printf_max_op = FunctionOperator("printf", arg_map = {0: "\"max %s error is reached at input number %s \\n \"" % (self.function_name, "%d"), 1: FO_Arg(0)}, void_function = True, require_header=["stdio.h"])
      printf_max_function = FunctionObject("printf", [ML_Int32], ML_Void, printf_max_op)

      error_loop = Loop(
        ReferenceAssign(vi, Constant(0, precision = ML_Int32)),
        vi < test_num_cst,
        Statement(
          ConditionBlock(
            error_rel_comp,
            Statement(
              ReferenceAssign(max_error_relative, local_error_relative),
              ReferenceAssign(max_input, vi/loop_increment),
              ReferenceAssign(max_vi, vi),
              ReferenceAssign(max_result, local_result)
            ),
            Statement()),
          ConditionBlock(
            error_abs_comp,
            Statement(
              ReferenceAssign(max_error_absolute, local_error_absolute),
            ),
            Statement()),
          ReferenceAssign(vi, vi + loop_increment)
        ),
      )
      main_statement = Statement(
        ReferenceAssign(max_error_absolute, Constant(0, precision=self.precision)),
        ReferenceAssign(max_error_relative, Constant(0, precision=self.precision)),
        ReferenceAssign(max_input, Constant(0, precision=ML_Int32)),
        error_loop,
        printf_error_function(max_error_absolute, max_error_relative),
        printf_max_function(max_input),
        Return(max_error_relative),
      )
      return main_statement

  ## Generate a test wrapper for the @p self function
  #  @param test_num   number of test to perform
  #  @param test_range numeric range for test's inputs
  #  @param debug enable debug mode
  def generate_bench_wrapper(self, test_num = 10, loop_num=100000, test_ranges = [Interval(-1.0, 1.0)], debug = False):
    auto_test = CodeFunction("bench_wrapper", output_format=ML_Binary64)

    tested_function    = self.implementation.get_function_object()
    function_name      = self.implementation.get_name()

    failure_report_op       = FunctionOperator("report_failure")
    failure_report_function = FunctionObject("report_failure", [], ML_Void, failure_report_op)


    printf_success_op = FunctionOperator("printf", arg_map = {0: "\"test successful %s\\n\"" % function_name}, void_function = True) 
    printf_success_function = FunctionObject("printf", [], ML_Void, printf_success_op)

    test_total   = test_num
    # compute the number of standard test cases
    num_std_case = len(self.standard_test_cases)
    # add them to the total if standard test enabled
    if self.auto_test_std:
      test_total += num_std_case
    # round up the number of tests to the implementation vector-size
    diff        = self.get_vector_size() - (test_total % self.get_vector_size())
    test_total += diff
    test_num   += diff


    input_tables = [
      ML_NewTable(
        dimensions = [test_total],
        storage_precision = self.get_input_precision(i),
        tag = self.uniquify_name("input_table_arg%d" %i)
      )
      for i in range(self.arity)
    ]
    output_precision = FormatAttributeWrapper(self.precision, ["volatile"])
    ## (low, high) are store in output table
    output_table = ML_NewTable(dimensions = [test_total], storage_precision = output_precision, tag = self.uniquify_name("output_table"), empty = True)


    # TODO: factorize with auto-test wrapper generation function
    # random test cases
    for index, input_tuple in enumerate(self.generate_rand_input_iterator(test_total, test_ranges)):
      for in_id in range(self.arity):
        input_tables[in_id][index] = input_tuple[in_id]

    if self.implementation.get_output_format().is_vector_format():
      # vector implementation bench
      test_loop = self.get_vector_bench_wrapper(test_num, tested_function, input_tables, output_table)
    else: 
      # scalar implemetation bench
      test_loop = self.get_scalar_bench_wrapper(test_num, tested_function, input_tables, output_table)

    timer = Variable("timer", precision = ML_Int64, var_type = Variable.Local)
    printf_timing_op = FunctionOperator(
        "printf",
        arg_map = {
            0: "\"%s %%\"PRIi64\" elts computed in %%\"PRIi64\" cycles => %%.3f CPE \\n\"" % function_name,
            1: FO_Arg(0), 2: FO_Arg(1),
            3: FO_Arg(2)
        }, void_function = True,
        require_header=["stdio.h"]
    )
    printf_timing_function = FunctionObject("printf", [ML_Int64, ML_Int64, ML_Binary64], ML_Void, printf_timing_op)

    vj = Variable("j", precision=ML_Int32, var_type=Variable.Local)
    loop_num_cst = Constant(loop_num, precision=ML_Int32, tag="loop_num")
    loop_increment = 1

    # bench measure of clock per element
    cpe_measure = Division(
        Conversion(timer, precision=ML_Binary64),
        Constant(test_num * loop_num, precision=ML_Binary64),
        precision=ML_Binary64,
        tag="cpe_measure",
    )

    # common test scheme between scalar and vector functions
    test_scheme = Statement(
      self.processor.get_init_timestamp(),
      ReferenceAssign(timer, self.processor.get_current_timestamp()),
      Loop(
          ReferenceAssign(vj, Constant(0, precision=ML_Int32)),
          vj < loop_num_cst,
          Statement(
              test_loop,
              ReferenceAssign(vj, vj + loop_increment)
          )
      ),
      ReferenceAssign(timer,
        Subtraction(
          self.processor.get_current_timestamp(),
          timer,
          precision = ML_Int64
        )
      ),
      printf_timing_function(
        Constant(test_num * loop_num, precision = ML_Int64),
        timer,
        cpe_measure,
      ),
      Return(cpe_measure),
      # Return(Constant(0, precision = ML_Int32))
    )
    auto_test.set_scheme(test_scheme)
    return FunctionGroup([auto_test])


  ## generate a test loop for vector tests
  #  @param test_num number of elementary tests to be executed
  #  @param tested_function FunctionObject to be tested
  #  @param input_table ML_NewTable object containing test inputs
  #  @param output_table ML_NewTable object containing test outputs
  def get_vector_bench_wrapper(self, test_num, tested_function, input_tables, output_table):
    vector_format = self.implementation.get_output_format()
    assignation_statement = Statement()
    vi = Variable("i", precision = ML_Int32, var_type = Variable.Local)
    test_num_cst = Constant(test_num, precision = ML_Int32, tag = "test_num")

    # building inputs
    local_inputs = [
      Variable(
        "vec_x_{}".format(i) , 
        precision = vector_format, 
        var_type = Variable.Local
      ) for i in range(self.arity)
    ]
    for input_index, local_input in enumerate(local_inputs):
      assignation_statement.push(local_input)
      for k in range(self.get_vector_size()):
        elt_assign = ReferenceAssign(VectorElementSelection(local_input, k), TableLoad(input_tables[input_index], vi + k))
        assignation_statement.push(elt_assign)

    # computing results
    local_result = tested_function(*local_inputs)
    loop_increment = self.get_vector_size()

    store_statement = Statement()

    # comparison with expected
    for k in range(self.get_vector_size()):
      elt_result = VectorElementSelection(local_result, k)

      # TODO: change to use aligned linear vector store
      store_statement.push(
        TableStore(elt_result, output_table, vi + k, precision = ML_Void) 
      )

    test_loop = Loop(
      ReferenceAssign(vi, Constant(0, precision = ML_Int32)),
      vi < test_num_cst,
      Statement(
        assignation_statement,
        store_statement,
        ReferenceAssign(vi, vi + loop_increment)
      ),
    )
    return test_loop

  ## generate a bench loop for scalar tests
  #  @param test_num number of elementary tests to be executed
  #  @param tested_function FunctionObject to be tested
  #  @param input_tables list of ML_NewTable object containing test inputs
  #  @param output_table ML_NewTable object containing test outputs
  def get_scalar_bench_wrapper(self, test_num, tested_function, input_tables, output_table):
    assignation_statement = Statement()
    vi = Variable("i", precision = ML_Int32, var_type = Variable.Local)
    test_num_cst = Constant(test_num, precision = ML_Int32, tag = "test_num")

    local_inputs  = tuple(TableLoad(input_tables[in_id], vi) for in_id in range(self.arity))
    local_result = tested_function(*local_inputs)

    loop_increment = 1

    test_loop = Loop(
      ReferenceAssign(vi, Constant(0, precision = ML_Int32)),
      vi < test_num_cst,
      Statement(
        TableStore(local_result, output_table, vi, precision = ML_Void),
        ReferenceAssign(vi, vi + loop_increment)
      ),
    )
    return test_loop

  #@staticmethod
  def get_name(self):
    return self.function_name

  # list of input to be used for standard test validation
  standard_test_cases = []


## Function class builder to build ML_FunctionBasis
#  child class with specific function_name value
def ML_Function(name):
  new_class = type(name, (ML_FunctionBasis,), {"function_name": name})
  return new_class

# end of Doxygen's ml_function group
## @}

