"""
    This is the loadable seq2seq trainer library that is
    in charge of training details, loss compute, and statistics.
    See train.py for a use case of this library.

    Note: To make this a general library, we implement *only*
          mechanism things here(i.e. what to do), and leave the strategy
          things to users(i.e. how to do it). Also see train.py(one of the
          users of this library) for the strategy things we do.
"""

from __future__ import division

import onmt.inputters as inputters
import onmt.utils

from onmt.utils.logging import logger


def build_trainer(opt, device_id, model, fields,
                  optim, data_type, model_saver=None):
    """
    Simplify `Trainer` creation based on user `opt`s*

    Args:
        opt (:obj:`Namespace`): user options (usually from argument parsing)
        model (:obj:`onmt.models.NMTModel`): the model to train
        fields (dict): dict of fields
        optim (:obj:`onmt.utils.Optimizer`): optimizer used during training
        data_type (str): string describing the type of data
            e.g. "text", "img", "audio"
        model_saver(:obj:`onmt.models.ModelSaverBase`): the utility object
            used to save the model
    """
    train_loss, train_loss1, train_loss2 = onmt.utils.loss.build_loss_compute(
        model, fields["tgt"].vocab, opt)
    valid_loss, valid_loss1, valid_loss2 = onmt.utils.loss.build_loss_compute(
        model, fields["tgt"].vocab, opt, train=False)

    trunc_size = opt.truncated_decoder  # Badly named...
    shard_size = opt.max_generator_batches
    norm_method = opt.normalization
    grad_accum_count = opt.accum_count
    n_gpu = opt.world_size
    if device_id >= 0:
        gpu_rank = opt.gpu_ranks[device_id]
    else:
        gpu_rank = 0
        n_gpu = 0
    gpu_verbose_level = opt.gpu_verbose_level

    report_manager = onmt.utils.build_report_manager(opt)
    trainer = onmt.Trainer(model, train_loss,train_loss1, train_loss2, valid_loss, valid_loss1, valid_loss2, optim, trunc_size,
                           shard_size, data_type, norm_method,
                           grad_accum_count, n_gpu, gpu_rank,
                           gpu_verbose_level, report_manager,
                           model_saver=model_saver)
    return trainer


class Trainer(object):
    """
    Class that controls the training process.

    Args:
            model(:py:class:`onmt.models.model.NMTModel`): translation model
                to train
            train_loss(:obj:`onmt.utils.loss.LossComputeBase`):
               training loss computation
            valid_loss(:obj:`onmt.utils.loss.LossComputeBase`):
               training loss computation
            optim(:obj:`onmt.utils.optimizers.Optimizer`):
               the optimizer responsible for update
            trunc_size(int): length of truncated back propagation through time
            shard_size(int): compute loss in shards of this size for efficiency
            data_type(string): type of the source input: [text|img|audio]
            norm_method(string): normalization methods: [sents|tokens]
            grad_accum_count(int): accumulate gradients this many times.
            report_manager(:obj:`onmt.utils.ReportMgrBase`):
                the object that creates reports, or None
            model_saver(:obj:`onmt.models.ModelSaverBase`): the saver is
                used to save a checkpoint.
                Thus nothing will be saved if this parameter is None
    """

    def __init__(self, model, train_loss, train_loss1, train_loss2, valid_loss, valid_loss1, valid_loss2, optim,
                 trunc_size=0, shard_size=32, data_type='text',
                 norm_method="sents", grad_accum_count=1, n_gpu=1, gpu_rank=1,
                 gpu_verbose_level=0, report_manager=None, model_saver=None):
        # Basic attributes.
        self.model = model
        self.train_loss = train_loss
        self.train_loss1 = train_loss1
        self.train_loss2 = train_loss2
        self.valid_loss = valid_loss
        self.valid_loss1 = valid_loss1
        self.valid_loss2 = valid_loss2
        self.optim = optim
        self.trunc_size = trunc_size
        self.shard_size = shard_size
        self.data_type = data_type
        self.norm_method = norm_method
        self.grad_accum_count = grad_accum_count
        self.n_gpu = n_gpu
        self.gpu_rank = gpu_rank
        self.gpu_verbose_level = gpu_verbose_level
        self.report_manager = report_manager
        self.model_saver = model_saver

        assert grad_accum_count > 0
        if grad_accum_count > 1:
            assert(self.trunc_size == 0), \
                """To enable accumulated gradients,
                   you must disable target sequence truncating."""

        # Set model in training mode.
        self.model.train()

    def train(self, train_iter_fct, valid_iter_fct, train_steps, valid_steps):
        """
        The main training loops.
        by iterating over training data (i.e. `train_iter_fct`)
        and running validation (i.e. iterating over `valid_iter_fct`

        Args:
            train_iter_fct(function): a function that returns the train
                iterator. e.g. something like
                train_iter_fct = lambda: generator(*args, **kwargs)
            valid_iter_fct(function): same as train_iter_fct, for valid data
            train_steps(int):
            valid_steps(int):
            save_checkpoint_steps(int):

        Return:
            None
        """
        logger.info('Start training...')

        step = self.optim._step + 1
        true_batchs = []
        accum = 0
        normalization = 0
        train_iter = train_iter_fct()

        total_stats = onmt.utils.Statistics()
        total_stats1 = onmt.utils.Statistics()
        total_stats2 = onmt.utils.Statistics()
        report_stats = onmt.utils.Statistics()
        report_stats1 = onmt.utils.Statistics()
        report_stats2 = onmt.utils.Statistics()
        self._start_report_manager(start_time=total_stats.start_time)

        while step <= train_steps:

            reduce_counter = 0
            for i, batch in enumerate(train_iter):
                if self.n_gpu == 0 or (i % self.n_gpu == self.gpu_rank):
                    if self.gpu_verbose_level > 1:
                        logger.info("GpuRank %d: index: %d accum: %d"
                                    % (self.gpu_rank, i, accum))

                    true_batchs.append(batch)

                    if self.norm_method == "tokens":
                        num_tokens = batch.tgt[1:].ne(
                            self.train_loss.padding_idx).sum()
                        normalization += num_tokens.item()
                    else:
                        normalization += batch.batch_size
                    accum += 1
                    if accum == self.grad_accum_count:
                        reduce_counter += 1
                        if self.gpu_verbose_level > 0:
                            logger.info("GpuRank %d: reduce_counter: %d \
                                        n_minibatch %d"
                                        % (self.gpu_rank, reduce_counter,
                                           len(true_batchs)))
                        if self.n_gpu > 1:
                            normalization = sum(onmt.utils.distributed
                                                .all_gather_list
                                                (normalization))

                        self._gradient_accumulation(
                            true_batchs, normalization, total_stats,total_stats1,total_stats2,
                            report_stats,report_stats1,report_stats2)

                        report_stats = self._maybe_report_training(
                            step, train_steps,
                            self.optim.learning_rate,
                            report_stats)
                        report_stats1 = self._maybe_report_training(
                            step, train_steps,
                            self.optim.learning_rate,
                            report_stats1)
                        report_stats2 = self._maybe_report_training(
                            step, train_steps,
                            self.optim.learning_rate,
                            report_stats2)

                        true_batchs = []
                        accum = 0
                        normalization = 0
                        if (step % valid_steps == 0):
                            if self.gpu_verbose_level > 0:
                                logger.info('GpuRank %d: validate step %d'
                                            % (self.gpu_rank, step))
                            valid_iter = valid_iter_fct()
                            valid_stats,valid_stats1,valid_stats2 = self.validate(valid_iter)
                            if self.gpu_verbose_level > 0:
                                logger.info('GpuRank %d: gather valid stat \
                                            step %d' % (self.gpu_rank, step))
                            valid_stats = self._maybe_gather_stats(valid_stats)
                            valid_stats1 = self._maybe_gather_stats(valid_stats1)
                            valid_stats2 = self._maybe_gather_stats(valid_stats2)
                            if self.gpu_verbose_level > 0:
                                logger.info('GpuRank %d: report stat step %d'
                                            % (self.gpu_rank, step))
                            self._report_step(self.optim.learning_rate,
                                              step, valid_stats=valid_stats)
                            self._report_step(self.optim.learning_rate,
                                              step, valid_stats=valid_stats1)
                            self._report_step(self.optim.learning_rate,
                                              step, valid_stats=valid_stats2)

                        if self.gpu_rank == 0:
                            self._maybe_save(step)
                        step += 1
                        if step > train_steps:
                            break
            if self.gpu_verbose_level > 0:
                logger.info('GpuRank %d: we completed an epoch \
                            at step %d' % (self.gpu_rank, step))
            train_iter = train_iter_fct()

        return total_stats,total_stats1,total_stats2

    def validate(self, valid_iter):
        """ Validate model.
            valid_iter: validate data iterator
        Returns:
            :obj:`nmt.Statistics`: validation loss statistics
        """
        # Set model in validating mode.
        self.model.eval()

        stats = onmt.utils.Statistics()
        stats1 = onmt.utils.Statistics()
        stats2 = onmt.utils.Statistics()

        for batch in valid_iter:
            src = inputters.make_features(batch, 'src', self.data_type)
            src1 = inputters.make_features(batch, 'src1', self.data_type)
            if self.data_type == 'text':
                _, src_lengths = batch.src
                _, src_lengths1 = batch.src1
            elif self.data_type == 'audio':
                src_lengths = batch.src_lengths
            else:
                src_lengths = None

            tgt = inputters.make_features(batch, 'tgt')
            tgt1 = inputters.make_features(batch, 'tgt1')
            tgt2 = inputters.make_features(batch, 'tgt2')
            tgt1_index = batch.tgt1_index
            tgt2_index = batch.tgt2_index

            # F-prop through the model.
            outputs, attns,dec_out_1, attns_1, dec_out_2, attns_2 = self.model(src, src1, tgt, tgt1, tgt2, tgt1_index, tgt2_index, src_lengths,src_lengths1)

            # Compute loss.
            batch_stats = self.valid_loss.monolithic_compute_loss(
                batch, batch.tgt, outputs, attns,batch.alignment, batch.src_map)
            batch_stats1 = self.valid_loss1.monolithic_compute_loss(
                batch,batch.tgt1, dec_out_1, attns_1,batch.alignment1, batch.src1_map)
            batch_stats2 = self.valid_loss2.monolithic_compute_loss(
                batch,batch.tgt2, dec_out_2, attns_2,batch.alignment2, batch.src1_map)

            # Update statistics.
            stats.update(batch_stats)
            stats1.update(batch_stats1)
            stats2.update(batch_stats2)

        # Set model back to training mode.
        self.model.train()

        return stats, stats1, stats2

    def _gradient_accumulation(self, true_batchs, normalization, total_stats, total_stats1, total_stats2,
                               report_stats,report_stats1,report_stats2):
        if self.grad_accum_count > 1:
            self.model.zero_grad()

        for batch in true_batchs:


            # dec_state = None
            src = inputters.make_features(batch, 'src', self.data_type)
            src1 = inputters.make_features(batch, 'src1', self.data_type)

            if self.data_type == 'text':
                _, src_lengths = batch.src
                _, src_lengths1 = batch.src1
                report_stats.n_src_words += src_lengths.sum().item()
                report_stats1.n_src_words += src_lengths1.sum().item()
                report_stats2.n_src_words += src_lengths1.sum().item()
            elif self.data_type == 'audio':
                src_lengths = batch.src_lengths
            else:
                src_lengths = None

            tgt_outer = inputters.make_features(batch, 'tgt')
            tgt_outer1 = inputters.make_features(batch, 'tgt1')
            tgt_outer2 = inputters.make_features(batch, 'tgt2')
            tgt1_index = batch.tgt1_index
            tgt2_index = batch.tgt2_index


            tgt = tgt_outer
            tgt1 = tgt_outer1
            tgt2 = tgt_outer2
            '''print("src")
            print(src)
            print("src1")
            print(src1)
            print("tgt")
            print(tgt)
            print("tgt1")
            print(tgt1)
            
            print(tgt1_index)
            print(tgt2_index)
            print(src_lengths)
            print(src_lengths1)'''

            # 2. F-prop all but generator.
            if self.grad_accum_count == 1:
                self.model.zero_grad()
            outputs, attns, dec_out_1, attns_1, dec_out_2, attns_2 = \
                self.model(src, src1, tgt, tgt1, tgt2, tgt1_index, tgt2_index, src_lengths, src_lengths1)

            # 3. Compute loss in shards for memory efficiency.
            loss, batch_stats = self.train_loss.sharded_compute_loss(
                batch,
                tgt, outputs, attns, 0,
                batch.tgt.size(0), self.shard_size, normalization, batch.alignment, batch.src_map)




            loss1, batch_stats1 = self.train_loss1.sharded_compute_loss(
                batch,
                tgt1, dec_out_1, attns_1, 0,
                batch.tgt1.size(0), self.shard_size, normalization, batch.alignment1, batch.src1_map)

            loss2, batch_stats2 = self.train_loss2.sharded_compute_loss(
                batch, tgt2, dec_out_2, attns_2, 0,
                batch.tgt2.size(0), self.shard_size, normalization, batch.alignment2, batch.src1_map)

            (loss+loss1+loss2).div(float(normalization)).backward()#
            total_stats.update(batch_stats)
            total_stats1.update(batch_stats1)
            total_stats2.update(batch_stats2)
            report_stats.update(batch_stats)
            report_stats1.update(batch_stats1)
            report_stats2.update(batch_stats2)

            # 4. Update the parameters and statistics.
            if self.grad_accum_count == 1:
                # Multi GPU gradient gather
                if self.n_gpu > 1:
                    grads = [p.grad.data for p in self.model.parameters()
                             if p.requires_grad
                             and p.grad is not None]
                    onmt.utils.distributed.all_reduce_and_rescale_tensors(
                        grads, float(1))
                self.optim.step()

            # If truncated, don't backprop fully.
            # TO CHECK
            # if dec_state is not None:
            #    dec_state.detach()
            if self.model.decoder.state is not None:
                self.model.decoder.detach_state()

        # in case of multi step gradient accumulation,
        # update only after accum batches
        if self.grad_accum_count > 1:
            if self.n_gpu > 1:
                grads = [p.grad.data for p in self.model.parameters()
                         if p.requires_grad
                         and p.grad is not None]
                onmt.utils.distributed.all_reduce_and_rescale_tensors(
                    grads, float(1))
            self.optim.step()

    def _start_report_manager(self, start_time=None):
        """
        Simple function to start report manager (if any)
        """
        if self.report_manager is not None:
            if start_time is None:
                self.report_manager.start()
            else:
                self.report_manager.start_time = start_time

    def _maybe_gather_stats(self, stat):
        """
        Gather statistics in multi-processes cases

        Args:
            stat(:obj:onmt.utils.Statistics): a Statistics object to gather
                or None (it returns None in this case)

        Returns:
            stat: the updated (or unchanged) stat object
        """
        if stat is not None and self.n_gpu > 1:
            return onmt.utils.Statistics.all_gather_stats(stat)
        return stat

    def _maybe_report_training(self, step, num_steps, learning_rate,
                               report_stats):
        """
        Simple function to report training stats (if report_manager is set)
        see `onmt.utils.ReportManagerBase.report_training` for doc
        """
        if self.report_manager is not None:
            return self.report_manager.report_training(
                step, num_steps, learning_rate, report_stats,
                multigpu=self.n_gpu > 1)

    def _report_step(self, learning_rate, step, train_stats=None,
                     valid_stats=None):
        """
        Simple function to report stats (if report_manager is set)
        see `onmt.utils.ReportManagerBase.report_step` for doc
        """
        if self.report_manager is not None:
            return self.report_manager.report_step(
                learning_rate, step, train_stats=train_stats,
                valid_stats=valid_stats)

    def _maybe_save(self, step):
        """
        Save the model if a model saver is set
        """
        if self.model_saver is not None:
            self.model_saver.maybe_save(step)
