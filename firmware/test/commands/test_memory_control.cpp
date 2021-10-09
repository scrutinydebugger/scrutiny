#include <gtest/gtest.h>
#include "scrutiny.h"
#include "scrutiny_test.h"

class TestMemoryControl : public ScrutinyTest
{
protected:
	scrutiny::Timebase tb;
	scrutiny::MainHandler scrutiny_handler;
	scrutiny::Config config;

	virtual void SetUp()
	{
		scrutiny_handler.init(&config);
		scrutiny_handler.comm()->connect();
	}
};

// ================================= Read =================================
/*
	Read a single memory block and expect a response with a valid content
*/
TEST_F(TestMemoryControl, TestReadSingleAddress)
{
	// Building request
	uint8_t data_buf[] = { 0x11, 0x22, 0x33 };
	constexpr uint32_t addr_size = sizeof(std::uintptr_t);
	constexpr uint16_t data_size = sizeof(data_buf);
	uint8_t request_data[8 + addr_size + 2] = { 3,1,0, addr_size + 2 };
	unsigned int index = 4;
	index += encode_addr(&request_data[index], data_buf);
	request_data[index++] = (data_size >> 8) & 0xFF;
	request_data[index++] = (data_size >> 0) & 0xFF;
	add_crc(request_data, sizeof(request_data) - 4);

	// Building expected response
	uint8_t tx_buffer[32];
	constexpr uint16_t datalen = addr_size + 2 + data_size;
	uint8_t expected_response[9 + datalen] = { 0x83, 1, 0, 0, datalen };
	index = 5;
	index += encode_addr(&expected_response[index], data_buf);
	expected_response[index++] = (data_size >> 8) & 0xFF;
	expected_response[index++] = (data_size >> 0) & 0xFF;
	std::memcpy(&expected_response[index], data_buf, data_size);
	add_crc(expected_response, sizeof(expected_response) - 4);

	// Process
	scrutiny_handler.comm()->receive_data(request_data, sizeof(request_data));
	scrutiny_handler.process(0);

	uint32_t n_to_read = scrutiny_handler.comm()->data_to_send();
	ASSERT_GT(n_to_read, 0u);
	ASSERT_LT(n_to_read, sizeof(tx_buffer));
	EXPECT_EQ(n_to_read, sizeof(expected_response));

	uint32_t nread = scrutiny_handler.comm()->pop_data(tx_buffer, n_to_read);
	EXPECT_EQ(nread, n_to_read);

	ASSERT_BUF_EQ(tx_buffer, expected_response, sizeof(expected_response));
}

/*
	Reads a 3 memory block and expect a response with a valid content
*/
TEST_F(TestMemoryControl, TestReadMultipleAddress)
{
	uint8_t data_buf1[] = { 0x11, 0x22, 0x33 };
	uint8_t data_buf2[] = { 0x44, 0x55, 0x66, 0x77 };
	uint8_t data_buf3[] = { 0x88, 0x99 };
	uint8_t tx_buffer[64];
	constexpr uint32_t addr_size = sizeof(std::uintptr_t);
	constexpr uint16_t data_size1 = sizeof(data_buf1);
	constexpr uint16_t data_size2 = sizeof(data_buf2);
	constexpr uint16_t data_size3 = sizeof(data_buf3);
	constexpr uint16_t datalen_req = (addr_size + 2) * 3;

	// Building request
	uint8_t request_data[8 + datalen_req] = { 3,1,0, datalen_req };
	unsigned int index = 4;
	index += encode_addr(&request_data[index], data_buf1);
	request_data[index++] = (data_size1 >> 8) & 0xFF;
	request_data[index++] = (data_size1 >> 0) & 0xFF;
	index += encode_addr(&request_data[index], data_buf2);
	request_data[index++] = (data_size2 >> 8) & 0xFF;
	request_data[index++] = (data_size2 >> 0) & 0xFF;
	index += encode_addr(&request_data[index], data_buf3);
	request_data[index++] = (data_size3 >> 8) & 0xFF;
	request_data[index++] = (data_size3 >> 0) & 0xFF;
	add_crc(request_data, sizeof(request_data) - 4);

	// Building expected_response
	constexpr uint16_t datalen_resp = (addr_size + 2) * 3 + data_size1 + data_size2 + data_size3;
	uint8_t expected_response[9 + datalen_resp] = { 0x83, 1, 0, 0, datalen_resp };
	index = 5;
	index += encode_addr(&expected_response[index], data_buf1);
	expected_response[index++] = (data_size1 >> 8) & 0xFF;
	expected_response[index++] = (data_size1 >> 0) & 0xFF;
	std::memcpy(&expected_response[index], data_buf1, data_size1);
	index += data_size1;
	index += encode_addr(&expected_response[index], data_buf2);
	expected_response[index++] = (data_size2 >> 8) & 0xFF;
	expected_response[index++] = (data_size2 >> 0) & 0xFF;
	std::memcpy(&expected_response[index], data_buf2, data_size2);
	index += data_size2;
	index += encode_addr(&expected_response[index], data_buf3);
	expected_response[index++] = (data_size3 >> 8) & 0xFF;
	expected_response[index++] = (data_size3 >> 0) & 0xFF;
	std::memcpy(&expected_response[index], data_buf3, data_size3);
	index += data_size3;
	add_crc(expected_response, sizeof(expected_response) - 4);

	// Processing
	scrutiny_handler.comm()->receive_data(request_data, sizeof(request_data));
	scrutiny_handler.process(0);

	uint32_t n_to_read = scrutiny_handler.comm()->data_to_send();
	ASSERT_GT(n_to_read, 0u);
	ASSERT_LT(n_to_read, sizeof(tx_buffer));
	EXPECT_EQ(n_to_read, sizeof(expected_response));

	uint32_t nread = scrutiny_handler.comm()->pop_data(tx_buffer, n_to_read);
	EXPECT_EQ(nread, n_to_read);

	ASSERT_BUF_EQ(tx_buffer, expected_response, sizeof(expected_response));
}

/*
	Sends multiple requests with an invalid amount of data and expect to receive and "InvalidRequest" response
*/
TEST_F(TestMemoryControl, TestReadAddressInvalidRequest)
{
	constexpr uint32_t addr_size = sizeof(void*);
	const scrutiny::Protocol::CommandId cmd = scrutiny::Protocol::eCmdMemoryControl;
	const scrutiny::Protocol::MemoryControl::Subfunction subfn = scrutiny::Protocol::MemoryControl::eSubfnRead;
	const scrutiny::Protocol::ResponseCode code = scrutiny::Protocol::eResponseCode_InvalidRequest;

	uint8_t tx_buffer[32];

	// Building request
	uint8_t request_data[64] = { cmd, subfn };
	uint16_t length_to_receive;
	for (unsigned int i = 0; i < 32; i++)
	{
		if (i % (addr_size + 2) == 0)
		{
			// This is a valid request. We skip it
			continue;
		}

		uint16_t length_to_test = static_cast<uint16_t>(i);
		length_to_receive = 8 + length_to_test;
		request_data[2] = static_cast<uint8_t>(length_to_test >> 8);	// Encode length
		request_data[3] = static_cast<uint8_t>(length_to_test);
		add_crc(request_data, length_to_receive - 4);

		scrutiny_handler.comm()->receive_data(request_data, length_to_receive);
		scrutiny_handler.process(0);

		uint32_t n_to_read = scrutiny_handler.comm()->data_to_send();
		ASSERT_GT(n_to_read, 0u) << "[ i=" << static_cast<uint32_t>(i) << "]";
		ASSERT_LT(n_to_read, sizeof(tx_buffer)) << "[i=" << static_cast<uint32_t>(i) << "]";
		scrutiny_handler.comm()->pop_data(tx_buffer, n_to_read);
		// Now we expect an InvalidRequest response
		ASSERT_TRUE(IS_PROTOCOL_RESPONSE(tx_buffer, cmd, subfn, code)) << "[i=" << static_cast<uint32_t>(i) << "]";
		scrutiny_handler.process(0);
	}
}

/*
	Sends multiple request for 2 blocks of data. The first block will almost fill the transmit buffer.
	Depending on the size of the second block, we expect either a valid respons or an "overflow" response if we ask
	for more data than what can fir in the TX buffer.
*/

TEST_F(TestMemoryControl, TestReadAddressOverflow)
{
	constexpr uint32_t addr_size = sizeof(void*);
	const scrutiny::Protocol::CommandId cmd = scrutiny::Protocol::eCmdMemoryControl;
	const scrutiny::Protocol::MemoryControl::Subfunction subfn = scrutiny::Protocol::MemoryControl::eSubfnRead;
	const scrutiny::Protocol::ResponseCode overflow = scrutiny::Protocol::eResponseCode_Overflow;
	const scrutiny::Protocol::ResponseCode ok = scrutiny::Protocol::eResponseCode_OK;

	uint8_t tx_buffer[SCRUTINY_TX_BUFFER_SIZE * 2];
	uint8_t some_buffer[SCRUTINY_TX_BUFFER_SIZE] = { 0 };
	uint16_t buf1_size = SCRUTINY_TX_BUFFER_SIZE - (addr_size + 2) * 2 - 1;	// We fill all the buffer minus 1 byte.

	// Building request
	uint8_t request_data[64] = { cmd, subfn, 0, (addr_size + 2) * 2 };
	unsigned int index = 4;
	index += encode_addr(&request_data[index], &some_buffer);
	request_data[index++] = static_cast<uint8_t>(buf1_size >> 8);
	request_data[index++] = static_cast<uint8_t>(buf1_size >> 0);

	index += encode_addr(&request_data[index], &some_buffer);	// 2nd block

	uint16_t length_to_receive;
	// Increase length of 2nd block of data.
	for (unsigned int length = 0; length < 4; length++)
	{
		length_to_receive = 8 + (addr_size + 2) * 2;

		request_data[index + 0] = static_cast<uint8_t>(length >> 8);	// encode length of 2nd block
		request_data[index + 1] = static_cast<uint8_t>(length >> 0);
		add_crc(request_data, length_to_receive - 4);

		scrutiny_handler.comm()->receive_data(request_data, length_to_receive);
		scrutiny_handler.process(0);

		uint32_t n_to_read = scrutiny_handler.comm()->data_to_send();
		ASSERT_GT(n_to_read, 0u);
		ASSERT_LT(n_to_read, sizeof(tx_buffer));
		scrutiny_handler.comm()->pop_data(tx_buffer, n_to_read);

		if (length < 2)	// Below 2, we don't overflow the tx buffer
		{
			ASSERT_TRUE(IS_PROTOCOL_RESPONSE(tx_buffer, cmd, subfn, ok)) << "[length=" << static_cast<uint32_t>(length) << "]";
		}
		else // We should have overflown the tx buffer here.
		{
			ASSERT_TRUE(IS_PROTOCOL_RESPONSE(tx_buffer, cmd, subfn, overflow)) << "[length=" << static_cast<uint32_t>(length) << "]";
		}
		scrutiny_handler.process(0);
	}
}


TEST_F(TestMemoryControl, TestReadForbiddenAddress)
{
	const scrutiny::Protocol::CommandId cmd = scrutiny::Protocol::eCmdMemoryControl;
	const scrutiny::Protocol::MemoryControl::Subfunction subfn = scrutiny::Protocol::MemoryControl::eSubfnRead;
	const scrutiny::Protocol::ResponseCode forbidden = scrutiny::Protocol::eResponseCode_Forbidden;
	const scrutiny::Protocol::ResponseCode ok = scrutiny::Protocol::eResponseCode_OK;

	uint8_t tx_buffer[32];
	uint8_t buf[] = { 0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x0e, 0x0f };
	scrutiny::Config new_config;
	// indices [6,7,8,9] are forbidden
	uint64_t start = reinterpret_cast<uint64_t>(buf) + 6;
	uint64_t end = start + 4;
	new_config.add_forbidden_address_range(start, end);
	scrutiny_handler.init(&new_config);
	scrutiny_handler.comm()->connect();

	constexpr uint8_t datalen = sizeof(void*) + 2;
	uint8_t request_data[8 + datalen] = {3,1,0, datalen };
	uint16_t window_size = 4;
	unsigned int index = 0;
	for (unsigned int i = 0; i < sizeof(buf) - window_size; i++)
	{
		void* read_addr = reinterpret_cast<void*>(reinterpret_cast<std::uintptr_t>(buf) + i);
		index = 4;
		index += encode_addr(&request_data[4], read_addr);
		request_data[index + 0] = static_cast<uint8_t>(window_size >> 8);
		request_data[index + 1] = static_cast<uint8_t>(window_size >> 0);
		add_crc(request_data, sizeof(request_data) - 4);

		scrutiny_handler.comm()->receive_data(request_data, sizeof(request_data));
		scrutiny_handler.process(0);

		uint32_t n_to_read = scrutiny_handler.comm()->data_to_send();
		ASSERT_GT(n_to_read, 0u);
		ASSERT_LT(n_to_read, sizeof(tx_buffer));
		scrutiny_handler.comm()->pop_data(tx_buffer, n_to_read);

		if (i < 2 || i > 10)	// Sliding window is completely out of forbidden region
		{
			ASSERT_TRUE(IS_PROTOCOL_RESPONSE(tx_buffer, cmd, subfn, ok)) << "[i=" << static_cast<uint32_t>(i) << "]";
		}
		else // We expect to be refused access to buffer
		{
			ASSERT_TRUE(IS_PROTOCOL_RESPONSE(tx_buffer, cmd, subfn, forbidden)) << "[i=" << static_cast<uint32_t>(i) << "]";
		}
		scrutiny_handler.process(0);
	}

}


// ================================= Write =================================


/*
	Read a single memory block and expect a response with a valid content
*/
TEST_F(TestMemoryControl, TestWriteSingleAddress)
{
	
	uint8_t buffer[] = { 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0a };
	uint8_t data_to_write[] = { 0x11, 0x22, 0x33, 0x44 };
	uint8_t expected_output_buffer[] = { 0x11, 0x22, 0x33, 0x44, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0a };

	// Building request
	constexpr uint32_t addr_size = sizeof(std::uintptr_t);
	constexpr uint16_t datalen_req = addr_size+2+sizeof(data_to_write);
	uint8_t request_data[8 + datalen_req] = { 3,2,0, datalen_req };
	unsigned int index = 4;
	index += encode_addr(&request_data[index], buffer);
	request_data[index++] = (sizeof(data_to_write) >> 8) & 0xFF;
	request_data[index++] = (sizeof(data_to_write) >> 0) & 0xFF;
	std::memcpy(&request_data[index], data_to_write, sizeof(data_to_write));
	add_crc(request_data, sizeof(request_data) - 4);

	// Building expected response
	uint8_t tx_buffer[32];
	constexpr uint16_t datalen_resp = addr_size +2;
	uint8_t expected_response[9 + datalen_resp] = { 0x83, 2, 0, 0, datalen_resp };
	index = 5;
	index += encode_addr(&expected_response[index], buffer);
	expected_response[index++] = (sizeof(data_to_write) >> 8) & 0xFF;
	expected_response[index++] = (sizeof(data_to_write) >> 0) & 0xFF;
	add_crc(expected_response, sizeof(expected_response) - 4);

	// Process
	scrutiny_handler.comm()->receive_data(request_data, sizeof(request_data));
	scrutiny_handler.process(0);

	uint32_t n_to_read = scrutiny_handler.comm()->data_to_send();
	ASSERT_GT(n_to_read, 0u);
	ASSERT_LT(n_to_read, sizeof(tx_buffer));
	EXPECT_EQ(n_to_read, sizeof(expected_response));

	uint32_t nread = scrutiny_handler.comm()->pop_data(tx_buffer, n_to_read);
	EXPECT_EQ(nread, n_to_read);

	ASSERT_BUF_EQ(tx_buffer, expected_response, sizeof(expected_response));
	ASSERT_BUF_EQ(buffer, expected_output_buffer, sizeof(expected_output_buffer));
}
