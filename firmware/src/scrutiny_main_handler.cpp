#include <cstring>

#include "scrutiny_main_handler.h"
#include "scrutiny_software_id.h"

namespace scrutiny
{


void MainHandler::init(Config *config)
{
    m_processing_request = false;
    m_comm_handler.init(&m_timebase);

    m_config.copy_from(config);
}

void MainHandler::process(uint32_t timestep_us)
{
    m_timebase.step(timestep_us);
    m_comm_handler.process();

    if (m_comm_handler.request_received() && !m_processing_request)
    {   
        m_processing_request = true;
        Protocol::Response *response = m_comm_handler.prepare_response();
        process_request(m_comm_handler.get_request(), response);
        if (response->valid)
        {
            m_comm_handler.send_response(response);
        }
    }

    if (m_processing_request)
    {
        if (!m_comm_handler.transmitting())  
        {
            m_comm_handler.request_processed(); // Allow reception of next request
            m_processing_request = false;
        }
    }
}


void MainHandler::process_request(Protocol::Request *request, Protocol::Response *response)
{
    Protocol::ResponseCode code = Protocol::eResponseCode_FailureToProceed;
    response->reset();

    if (!request->valid)
        return;

    response->command_id = request->command_id;
    response->subfunction_id = request->subfunction_id;
    response->response_code = Protocol::eResponseCode_OK;
    response->valid = true;

    switch (request->command_id)
    {
        // =============    GetInfo        ============
        case Protocol::eCmdGetInfo:
            code = process_get_info(request, response);
            break;

        // =============    CommControl    ============
        case Protocol::eCmdCommControl:
            code = process_comm_control(request, response);
            break;

        // =============    MemoryControl  ============
        case Protocol::eCmdMemoryControl:
            break;

        // =============    DataLogControl  ===========
        case Protocol::eCmdDataLogControl:
            break;

        // =============    UserCommand     ===========
        case Protocol::eCmdUserCommand:
            break;

        // ============================================
        default:
            response->response_code = Protocol::eResponseCode_UnsupportedFeature;
            break;
    }

    response->response_code = static_cast<uint8_t>(code);
    if (code != Protocol::eResponseCode_OK)
    {
        response->data_length = 0;
    }
}

Protocol::ResponseCode MainHandler::process_get_info(Protocol::Request *request, Protocol::Response *response)
{
    Protocol::ResponseData response_data;
    Protocol::ResponseCode code = Protocol::eResponseCode_FailureToProceed;

    switch (request->subfunction_id)
    {
        // ===========  Get Protocol Version     ==========
        case Protocol::GetInfo::eSubfnGetProtocolVersion:
            response_data.get_info.get_protocol_version.major = PROTOCOL_VERSION_MAJOR(ACTUAL_PROTOCOL_VERSION);
            response_data.get_info.get_protocol_version.minor = PROTOCOL_VERSION_MINOR(ACTUAL_PROTOCOL_VERSION);
            code = m_codec.encode_response_protocol_version(&response_data, response);
            break;

        // ===========  Get Software ID          ==========
        case Protocol::GetInfo::eSubfnGetSoftwareId:
            code = m_codec.encode_response_software_id(response);
            break;

        // ===========  Get Supported Features   ==========
        case Protocol::GetInfo::eSubfnGetSupportedFeatures:
            break;

        // =================================
        default:
            response->response_code = Protocol::eResponseCode_UnsupportedFeature;
            break;
    }

    return code;
}

Protocol::ResponseCode MainHandler::process_comm_control(Protocol::Request *request, Protocol::Response *response)
{
    Protocol::ResponseData response_data;
    Protocol::RequestData request_data;
    Protocol::ResponseCode code = Protocol::eResponseCode_FailureToProceed;

    switch (request->subfunction_id)
    {
        // =========== Discover ==========
        case Protocol::CommControl::eSubfnDiscover:
            code = m_codec.decode_request_comm_discover(request, &request_data);
            if (code != Protocol::eResponseCode_OK)
                break;

            std::memcpy(response_data.comm_control.discover.magic, Protocol::CommControl::DISCOVER_MAGIC, sizeof(Protocol::CommControl::DISCOVER_MAGIC));
            for (uint8_t i=0; i<sizeof(request_data.comm_control.discover.challenge); i++)
            {
                response_data.comm_control.discover.challenge_response[i] = ~request_data.comm_control.discover.challenge[i];
            }

            code = m_codec.encode_response_comm_discover(&response_data, response);
            break;

        // =========== Heartbeat ==========
        case Protocol::CommControl::eSubfnHeartbeat:
            code = m_codec.decode_request_comm_heartbeat(request, &request_data);
            if (code != Protocol::eResponseCode_OK)
                break;

            bool success;
            success = m_comm_handler.heartbeat(request_data.comm_control.heartbeat.challenge);
            if (!success)
            {
                code = Protocol::eResponseCode_InvalidRequest;
                break;
            }

            response_data.comm_control.heartbeat.challenge_response = ~request_data.comm_control.heartbeat.challenge;
            
            code = m_codec.encode_response_comm_heartbeat(&response_data, response);
            break;

        // =================================
        default:
            response->response_code = Protocol::eResponseCode_UnsupportedFeature;
            break;
    }

    return code;
}


/*
loop_id_t MainHandler::add_loop(LoopHandler* loop)
{
    return 0;
}

void MainHandler::process_loop(loop_id_t loop)
{
    
}
*/
}